from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "agent_inventory_diagnostic_v1"


def table_exists(conn, table_name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        (table_name,),
    ).fetchone() is not None


def table_columns(conn, table_name: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table_name})")}


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = reports_dir / f"{stamp}_agent_inventory_diagnostic"
    return base.with_suffix(".txt"), base.with_suffix(".json")


def fetch_registry(conn) -> list[dict[str, Any]]:
    if not table_exists(conn, "ml_agent_registry"):
        return []
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT *
            FROM ml_agent_registry
            ORDER BY dashboard_group, operational_state, agent_key
            """
        )
    ]


def fetch_latest_routing(conn) -> tuple[dict[str, Any] | None, dict[str, dict[str, Any]]]:
    if not table_exists(conn, "ml_agent_routing_runs") or not table_exists(conn, "ml_agent_routing_items"):
        return None, {}
    run = conn.execute(
        """
        SELECT *
        FROM ml_agent_routing_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if run is None:
        return None, {}
    by_agent: dict[str, dict[str, Any]] = {}
    for row in conn.execute(
        """
        SELECT
            route_agent_key AS agent_key,
            route_status,
            route_agent_type,
            COUNT(*) AS routed_rows,
            COUNT(DISTINCT segment_id) AS routed_segments,
            AVG(route_confidence) AS avg_confidence
        FROM ml_agent_routing_items
        WHERE run_id = ?
        GROUP BY route_agent_key, route_status, route_agent_type
        """,
        (int(run["id"]),),
    ):
        by_agent[str(row["agent_key"])] = dict(row)
    return dict(run), by_agent


def fetch_recommendations(conn) -> dict[str, dict[str, int]]:
    if not table_exists(conn, "ml_agent_recommendations"):
        return {}
    payload: dict[str, dict[str, int]] = {}
    for row in conn.execute(
        """
        SELECT
            proposed_agent_key AS agent_key,
            COUNT(*) AS recommendation_count,
            SUM(COALESCE(evidence_count, 0)) AS evidence_count,
            SUM(COALESCE(positive_count, 0)) AS positive_count,
            SUM(COALESCE(negative_count, 0)) AS negative_count,
            SUM(COALESCE(corrected_count, 0)) AS corrected_count
        FROM ml_agent_recommendations
        GROUP BY proposed_agent_key
        """
    ):
        payload[str(row["agent_key"])] = {
            "recommendation_count": int(row["recommendation_count"] or 0),
            "recommendation_evidence_count": int(row["evidence_count"] or 0),
            "recommendation_positive_count": int(row["positive_count"] or 0),
            "recommendation_negative_count": int(row["negative_count"] or 0),
            "recommendation_corrected_count": int(row["corrected_count"] or 0),
        }
    return payload


def evidence_status_sql(column_names: set[str]) -> str:
    checks: list[str] = []
    for col in ("checkpoint_allowed", "policy_allowed", "composed_allowed", "allowed", "released", "bridge_candidate"):
        if col in column_names:
            checks.append(f"COALESCE({col}, 0) = 1")
    for col in ("bridge_status", "shadow_status", "checkpoint_status", "policy_status", "status", "decision"):
        if col in column_names:
            checks.append(
                f"LOWER(COALESCE({col}, '')) LIKE '%ready%' "
                f"OR LOWER(COALESCE({col}, '')) LIKE '%allowed%' "
                f"OR LOWER(COALESCE({col}, '')) LIKE '%positive%' "
                f"OR LOWER(COALESCE({col}, '')) LIKE '%safe%'"
            )
    return " OR ".join(f"({check})" for check in checks) or "0"


def fetch_agent_evidence(conn) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    excluded_tables = {
        "ml_agent_registry",
        "ml_agent_routing_items",
        "ml_agent_routing_runs",
        "ml_agent_recommendations",
    }
    tables = [
        str(row["name"])
        for row in conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        )
    ]
    by_agent: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "evidence_rows": 0,
            "positive_like_rows": 0,
            "source_tables": Counter(),
        }
    )
    table_counts: dict[str, int] = {}
    for table in tables:
        if table in excluded_tables:
            continue
        columns = table_columns(conn, table)
        if "agent_key" not in columns:
            continue
        status_expr = evidence_status_sql(columns)
        try:
            rows = conn.execute(
                f"""
                SELECT
                    agent_key,
                    COUNT(*) AS evidence_rows,
                    SUM(CASE WHEN {status_expr} THEN 1 ELSE 0 END) AS positive_like_rows
                FROM {table}
                WHERE COALESCE(TRIM(agent_key), '') <> ''
                GROUP BY agent_key
                """
            ).fetchall()
        except Exception:
            continue
        if not rows:
            continue
        table_counts[table] = sum(int(row["evidence_rows"] or 0) for row in rows)
        for row in rows:
            key = str(row["agent_key"])
            by_agent[key]["evidence_rows"] += int(row["evidence_rows"] or 0)
            by_agent[key]["positive_like_rows"] += int(row["positive_like_rows"] or 0)
            by_agent[key]["source_tables"][table] += int(row["evidence_rows"] or 0)
    normalized: dict[str, dict[str, Any]] = {}
    for key, payload in by_agent.items():
        normalized[key] = {
            "evidence_rows": int(payload["evidence_rows"]),
            "positive_like_rows": int(payload["positive_like_rows"]),
            "source_tables": dict(payload["source_tables"].most_common(10)),
        }
    return normalized, table_counts


def classify_agent(row: dict[str, Any], *, routing: dict[str, Any], evidence: dict[str, Any], recommendation: dict[str, int]) -> str:
    status = str(row.get("status") or "")
    op = str(row.get("operational_state") or "")
    routed = int(routing.get("routed_rows") or 0)
    evidence_rows = int(evidence.get("evidence_rows") or 0)
    positive_rows = int(evidence.get("positive_like_rows") or 0)
    rec_evidence = int(recommendation.get("recommendation_evidence_count") or 0)
    if op == "authoritative":
        return "authoritative_core"
    if status == "active" and op == "operational":
        return "operational_core" if routed or evidence_rows else "operational_uninstrumented"
    if op in {"shadow", "dry_run", "experimental"} and (routed or evidence_rows or rec_evidence):
        return "lab_useful_evidence"
    if op == "candidate" and (positive_rows or rec_evidence):
        return "candidate_with_evidence"
    if status == "planned":
        return "planned_backlog"
    if evidence_rows or routed:
        return "legacy_or_unregistered_useful"
    return "stale_or_unproven"


def build_rows(
    registry: list[dict[str, Any]],
    routing: dict[str, dict[str, Any]],
    evidence: dict[str, dict[str, Any]],
    recommendations: dict[str, dict[str, int]],
) -> list[dict[str, Any]]:
    all_keys = set(routing) | set(evidence) | set(recommendations) | {str(row["agent_key"]) for row in registry}
    registry_by_key = {str(row["agent_key"]): row for row in registry}
    rows: list[dict[str, Any]] = []
    for key in sorted(all_keys):
        reg = registry_by_key.get(key, {})
        route = routing.get(key, {})
        evid = evidence.get(key, {})
        rec = recommendations.get(key, {})
        classification = classify_agent(reg, routing=route, evidence=evid, recommendation=rec)
        rows.append(
            {
                "agent_key": key,
                "agent_type": reg.get("agent_type") or route.get("route_agent_type") or "unregistered",
                "parent_agent_key": reg.get("parent_agent_key"),
                "status": reg.get("status") or "unregistered",
                "operational_state": reg.get("operational_state") or route.get("route_status") or "unregistered",
                "dashboard_group": reg.get("dashboard_group") or "unregistered",
                "decision_role": reg.get("decision_role"),
                "scope_group": reg.get("scope_group"),
                "classification": classification,
                "routed_rows": int(route.get("routed_rows") or 0),
                "routed_segments": int(route.get("routed_segments") or 0),
                "avg_confidence": route.get("avg_confidence"),
                "evidence_rows": int(evid.get("evidence_rows") or 0),
                "positive_like_rows": int(evid.get("positive_like_rows") or 0),
                "recommendation_count": int(rec.get("recommendation_count") or 0),
                "recommendation_evidence_count": int(rec.get("recommendation_evidence_count") or 0),
                "source_tables": evid.get("source_tables") or {},
            }
        )
    return rows


def main() -> dict[str, Any]:
    settings = db.load_settings()
    txt_path, json_path = report_paths(settings)
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        registry = fetch_registry(conn)
        routing_run, routing = fetch_latest_routing(conn)
        recommendations = fetch_recommendations(conn)
        evidence, table_counts = fetch_agent_evidence(conn)
        rows = build_rows(registry, routing, evidence, recommendations)

    classification_counts = Counter(row["classification"] for row in rows)
    status_counts = Counter((row["status"], row["operational_state"]) for row in rows)
    group_counts = Counter(row["dashboard_group"] for row in rows)
    routed_agents = sum(1 for row in rows if row["routed_rows"] > 0)
    evidence_agents = sum(1 for row in rows if row["evidence_rows"] > 0)
    positive_agents = sum(1 for row in rows if row["positive_like_rows"] > 0)
    no_signal_agents = [row for row in rows if row["routed_rows"] == 0 and row["evidence_rows"] == 0 and row["recommendation_evidence_count"] == 0]
    lab_useful = [row for row in rows if row["classification"] == "lab_useful_evidence"]
    stale = [row for row in rows if row["classification"] == "stale_or_unproven"]
    planned = [row for row in rows if row["classification"] == "planned_backlog"]

    payload = {
        "rule_version": RULE_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "routing_run": routing_run,
        "totals": {
            "registered_agents": len(registry),
            "observed_agent_keys": len(rows),
            "routed_agents": routed_agents,
            "evidence_agents": evidence_agents,
            "positive_like_evidence_agents": positive_agents,
            "no_signal_agents": len(no_signal_agents),
        },
        "classification_counts": dict(classification_counts),
        "status_operational_counts": {f"{status}/{op}": count for (status, op), count in status_counts.items()},
        "dashboard_group_counts": dict(group_counts),
        "top_evidence_agents": sorted(rows, key=lambda row: row["evidence_rows"], reverse=True)[:30],
        "top_routed_agents": sorted(rows, key=lambda row: row["routed_rows"], reverse=True)[:30],
        "lab_useful_agents": lab_useful[:80],
        "planned_backlog_agents": planned[:80],
        "stale_or_unproven_agents": stale[:80],
        "top_evidence_tables": dict(Counter(table_counts).most_common(30)),
        "rows": rows,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    lines = [
        "Agent Inventory Diagnostic",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {payload['generated_at']}",
        "",
        "Scope:",
        "- Counts registered agents, latest routing use, recommendation evidence and table-level agent evidence.",
        "- This is a governance/readiness diagnostic; it does not change source, output or production.",
        "",
        "Core totals:",
        f"- Registered agents: {len(registry):,}",
        f"- Observed agent keys including evidence-only/unregistered: {len(rows):,}",
        f"- Agents routed in latest architecture sample: {routed_agents:,}",
        f"- Agents with stored evidence rows: {evidence_agents:,}",
        f"- Agents with positive-like evidence rows: {positive_agents:,}",
        f"- Agents with no routing/evidence/recommendation signal: {len(no_signal_agents):,}",
        "",
        "Classification:",
        *[f"- {key}: {value:,}" for key, value in classification_counts.most_common()],
        "",
        "Status / operational state:",
        *[f"- {status}/{op}: {count:,}" for (status, op), count in status_counts.most_common()],
        "",
        "Dashboard groups:",
        *[f"- {key}: {value:,}" for key, value in group_counts.most_common()],
        "",
        "Top routed agents:",
        *[
            f"- {row['agent_key']} [{row['classification']}]: routed_rows={row['routed_rows']:,}, segments={row['routed_segments']:,}, evidence={row['evidence_rows']:,}"
            for row in sorted(rows, key=lambda item: item["routed_rows"], reverse=True)[:20]
        ],
        "",
        "Top evidence agents:",
        *[
            f"- {row['agent_key']} [{row['classification']}]: evidence={row['evidence_rows']:,}, positive_like={row['positive_like_rows']:,}, routed={row['routed_rows']:,}"
            for row in sorted(rows, key=lambda item: item["evidence_rows"], reverse=True)[:20]
        ],
        "",
        "Interpretation:",
        "- Operational agents are not all expected to have model-like evidence; some are hard gates or routers.",
        "- Shadow/candidate agents with evidence are useful laboratory neurons, not failures.",
        "- Planned agents with no evidence are backlog ideas; they should stay visually separate from operational health.",
        "- Stale/unproven agents are candidates for hiding, archiving, or revisiting only if a future blocker maps to them.",
        "",
        "Recommended governance:",
        "- Keep operational_core and authoritative_core in the main network view.",
        "- Show lab_useful_evidence in a Lab/Shadow layer with maturity and evidence counts.",
        "- Collapse planned_backlog by family unless actively selected.",
        "- Review stale_or_unproven only when a new issue family suggests reuse.",
        "",
        f"JSON: {json_path}",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("[agent_inventory_diagnostic] Diagnostic generated")
    print(f"[agent_inventory_diagnostic] Registered agents: {len(registry):,}")
    print(f"[agent_inventory_diagnostic] Observed agent keys: {len(rows):,}")
    print(f"[agent_inventory_diagnostic] Routed agents: {routed_agents:,}")
    print(f"[agent_inventory_diagnostic] Evidence agents: {evidence_agents:,}")
    print(f"[agent_inventory_diagnostic] No-signal agents: {len(no_signal_agents):,}")
    print(f"[agent_inventory_diagnostic] Report: {txt_path}")
    print(f"[agent_inventory_diagnostic] JSON: {json_path}")
    return payload


if __name__ == "__main__":
    main()
