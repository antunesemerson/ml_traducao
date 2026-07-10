from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_medium_dynamic_light_residual_registry_notes_update"
CONSOLIDATED_SUMMARY = Path(
    "reports/20260629_223142_627849_domain_policy_vote_candidate_medium_dynamic_light_residual_consolidated_diagnostic_summary.json"
)
ARTICLE_POLICY = "medium_dynamic_light_article_preposition_policy"
GETTER_POLICY = "medium_dynamic_light_getter_role_policy"
RESIDUAL_POLICY = "medium_dynamic_light_residual_parser_policy"
TARGET_AGENTS = (ARTICLE_POLICY, GETTER_POLICY, RESIDUAL_POLICY)


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def count_map(items: list[dict[str, Any]]) -> dict[str, int]:
    return {str(item["key"]): int(item["count"]) for item in items}


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def validate_summary(summary: dict[str, Any]) -> dict[str, Any]:
    if summary.get("mode") != "read_only_consolidated_diagnostic":
        raise SystemExit("summary mode guard failed")
    if int(summary.get("review_count") or 0) != 81:
        raise SystemExit("review_count guard failed")
    if int(summary.get("coverage_total") or 0) != 81 or summary.get("coverage_matches_residual") is not True:
        raise SystemExit("coverage guard failed")
    if summary.get("registry_guard_ok") is not True:
        raise SystemExit("registry_guard_ok guard failed")
    if summary.get("production_full_recommended_now") is not False:
        raise SystemExit("production_full_recommended_now guard failed")
    if summary.get("source_changed") is not False or summary.get("output_changed") is not False:
        raise SystemExit("source/output changed guard failed")

    absorbed = count_map(summary.get("absorbed_by_policy_counts") or [])
    final = count_map(summary.get("final_operational_counts") or [])
    inactive = count_map(summary.get("inactive_or_delegated_routes") or [])
    holds = count_map(summary.get("hold_breakdown") or [])
    expected_absorbed = {
        ARTICLE_POLICY: 36,
        GETTER_POLICY: 15,
    }
    expected_final = {
        ARTICLE_POLICY: 36,
        GETTER_POLICY: 15,
        "hold_context": 30,
    }
    expected_inactive = {
        "route_residual_generic_getter_role_unknown": 26,
        "route_residual_article_preposition_title_faith_culture": 21,
    }
    if absorbed != expected_absorbed:
        raise SystemExit(f"absorbed counts guard failed: {absorbed}")
    if final != expected_final:
        raise SystemExit(f"final counts guard failed: {final}")
    if inactive != expected_inactive:
        raise SystemExit(f"inactive route guard failed: {inactive}")
    if sum(holds.values()) != 30:
        raise SystemExit(f"hold total guard failed: {holds}")
    return {
        "absorbed": absorbed,
        "final": final,
        "inactive": inactive,
        "holds": holds,
    }


def fetch_agents(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM ml_agent_registry WHERE agent_key IN (?, ?, ?) ORDER BY agent_key",
        TARGET_AGENTS,
    ).fetchall()
    agents = {str(row["agent_key"]): dict(row) for row in rows}
    missing = [agent for agent in TARGET_AGENTS if agent not in agents]
    if missing:
        raise SystemExit(f"missing target registry agents: {missing}")
    return agents


def load_notes(row: dict[str, Any]) -> dict[str, Any]:
    return json.loads(row.get("notes_json") or "{}")


def validate_agent(row: dict[str, Any], notes: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    expected_fields = {
        "agent_type": "subcoordinator",
        "status": "active",
        "operational_state": "shadow",
        "decision_role": "route_and_split",
        "scope_group": "domain_policy_vote_candidate",
        "dashboard_group": "Issue Network",
    }
    for key, expected in expected_fields.items():
        if row.get(key) != expected:
            reasons.append(f"{row.get('agent_key')}_{key}_mismatch")
    for key in ("candidate_generation_allowed", "auto_apply_allowed", "lifecycle_allowed", "production_release_allowed"):
        if bool(notes.get(key)) is not False:
            reasons.append(f"{row.get('agent_key')}_{key}_not_false")
    return reasons


def build_update_payload(summary: dict[str, Any], counts: dict[str, Any]) -> dict[str, dict[str, Any]]:
    generated_at = datetime.now().isoformat(timespec="seconds")
    shared = {
        "source": SOURCE,
        "consolidated_summary_path": str(CONSOLIDATED_SUMMARY),
        "generated_at": generated_at,
        "read_only": True,
        "candidate_generation_allowed": False,
        "auto_apply_allowed": False,
        "lifecycle_allowed": False,
        "production_release_allowed": False,
        "segment_state_allowed": False,
        "reindex_allowed": False,
        "full_production_allowed": False,
        "guards": {
            "candidate_generation": False,
            "apply": False,
            "lifecycle": False,
            "segment_state": False,
            "reindex": False,
            "full_production": False,
        },
    }
    return {
        ARTICLE_POLICY: {
            **shared,
            "policy_role": "absorbs_residual_article_preposition_split_only_routes",
            "absorbed_split_only_count": counts["absorbed"][ARTICLE_POLICY],
            "absorbed_sources": {
                "article_preposition_title_faith_culture": 20,
                "generic_getter_article_preposition_context": 14,
                "culture_collective_object_preposition_context": 2,
            },
            "hold_count": 1,
            "inactive_source_routes": {
                "route_residual_article_preposition_title_faith_culture": 21,
            },
            "operational_note": (
                "Read-only split routing only; no candidate generation until article/gender/contractability guards exist."
            ),
        },
        GETTER_POLICY: {
            **shared,
            "policy_role": "absorbs_residual_getter_role_split_only_routes",
            "absorbed_split_only_count": counts["absorbed"][GETTER_POLICY],
            "absorbed_sources": {
                "generic_getter_roles": 11,
                "clergy_adherent_faith_or_adherent_routes": 3,
                "culture_collective_adjective_like": 1,
            },
            "operational_note": (
                "Read-only split routing only; absorbed routes stay non-candidate and non-lifecycle."
            ),
        },
        RESIDUAL_POLICY: {
            **shared,
            "policy_role": "residual_delegation_tracker",
            "review_count": int(summary["review_count"]),
            "final_operational_counts": counts["final"],
            "delegated_or_inactive_routes": counts["inactive"],
            "remaining_hold_breakdown": counts["holds"],
            "remaining_hold_total": sum(counts["holds"].values()),
            "route_state_overrides": {
                "route_residual_generic_getter_role_unknown": "delegated_inactive",
                "route_residual_article_preposition_title_faith_culture": "delegated_inactive",
            },
            "operational_note": (
                "Do not use generic_getter_role_unknown or article_preposition_title_faith_culture as active residual routes; "
                "delegate to existing splitters and keep unresolved residuals in hold."
            ),
        },
    }


def plan_updates(agents: dict[str, dict[str, Any]], payloads: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    guard_reasons: list[str] = []
    for agent_key in TARGET_AGENTS:
        row = agents[agent_key]
        notes = load_notes(row)
        guard_reasons.extend(validate_agent(row, notes))
        next_notes = dict(notes)
        next_notes["residual_medium_dynamic_light_consolidation"] = payloads[agent_key]
        for key in ("candidate_generation_allowed", "auto_apply_allowed", "lifecycle_allowed", "production_release_allowed"):
            next_notes[key] = False
        rows.append(
            {
                "agent_key": agent_key,
                "previous_notes_json": notes,
                "next_notes_json": next_notes,
                "changed": notes != next_notes,
            }
        )
    return rows, guard_reasons


def apply_updates(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> int:
    updated = 0
    now = db.utc_now()
    for row in rows:
        if not row["changed"]:
            continue
        conn.execute(
            """
            UPDATE ml_agent_registry
            SET notes_json = ?, updated_at = ?
            WHERE agent_key = ?
            """,
            (json.dumps(row["next_notes_json"], ensure_ascii=False, sort_keys=True), now, row["agent_key"]),
        )
        updated += 1
    return updated


def validate_after(conn: sqlite3.Connection) -> dict[str, Any]:
    agents = fetch_agents(conn)
    validation: dict[str, Any] = {}
    for agent_key, row in agents.items():
        notes = load_notes(row)
        payload = notes.get("residual_medium_dynamic_light_consolidation") or {}
        validation[agent_key] = {
            "exists": True,
            "has_consolidation_notes": bool(payload),
            "candidate_generation_allowed": bool(notes.get("candidate_generation_allowed")),
            "auto_apply_allowed": bool(notes.get("auto_apply_allowed")),
            "lifecycle_allowed": bool(notes.get("lifecycle_allowed")),
            "production_release_allowed": bool(notes.get("production_release_allowed")),
            "policy_role": payload.get("policy_role"),
        }
    return validation


def write_reports(
    *,
    mode: str,
    rows: list[dict[str, Any]],
    summary: dict[str, Any],
    guard_reasons: list[str],
    updated_count: int,
    validation_after: dict[str, Any],
) -> dict[str, str]:
    base = reports_dir() / f"{stamp()}_{SOURCE}_{mode}"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")

    for row in rows:
        row.pop("previous_notes_json", None)
        row.pop("next_notes_json", None)
    report_summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": mode,
        "input_summary_path": str(CONSOLIDATED_SUMMARY),
        "target_agents": list(TARGET_AGENTS),
        "planned_update_count": len(rows),
        "changed_count": sum(1 for row in rows if row["changed"]),
        "updated_count": updated_count,
        "guard_ok": not guard_reasons,
        "guard_reasons": guard_reasons,
        "absorbed_by_policy_counts": summary["absorbed_by_policy_counts"],
        "final_operational_counts": summary["final_operational_counts"],
        "inactive_or_delegated_routes": summary["inactive_or_delegated_routes"],
        "remaining_residual_families": summary["remaining_residual_families"],
        "validation_after": validation_after,
        "candidate_generation_count": 0,
        "apply_output_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "output_files": {},
    }
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    report_summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary_json": str(summary_path)}
    summary_path.write_text(json.dumps(report_summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "medium_dynamic_light residual registry notes update",
        "",
        f"mode: {mode}",
        f"guard_ok: {str(report_summary['guard_ok']).lower()}",
        f"planned_update_count: {report_summary['planned_update_count']}",
        f"changed_count: {report_summary['changed_count']}",
        f"updated_count: {updated_count}",
        "",
        "target_agents:",
    ]
    lines.extend(f"- {agent}" for agent in TARGET_AGENTS)
    lines.extend(["", "absorbed_by_policy_counts:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["absorbed_by_policy_counts"])
    lines.extend(["", "inactive_or_delegated_routes:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["inactive_or_delegated_routes"])
    lines.extend(
        [
            "",
            "gates:",
            "- candidate_generation: not_run",
            "- output_apply: not_run",
            "- lifecycle: not_run",
            "- segment_state: not_run",
            "- reindex: not_run",
            "- full_production: not_run",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary_json": str(summary_path)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    summary = read_json(CONSOLIDATED_SUMMARY)
    counts = validate_summary(summary)
    payloads = build_update_payload(summary, counts)

    if args.apply:
        with db.connect(db.load_settings()) as conn:
            conn.row_factory = sqlite3.Row
            agents = fetch_agents(conn)
            rows, guard_reasons = plan_updates(agents, payloads)
            if guard_reasons:
                raise SystemExit(f"registry guard failed: {guard_reasons}")
            updated_count = apply_updates(conn, rows)
            conn.commit()
            validation_after = validate_after(conn)
        mode = "apply"
    else:
        with connect_readonly() as conn:
            agents = fetch_agents(conn)
            rows, guard_reasons = plan_updates(agents, payloads)
            validation_after = validate_after(conn)
        updated_count = 0
        mode = "dry_run"

    output_files = write_reports(
        mode=mode,
        rows=rows,
        summary=summary,
        guard_reasons=guard_reasons,
        updated_count=updated_count,
        validation_after=validation_after,
    )
    print(json.dumps({
        "mode": mode,
        "target_agents": list(TARGET_AGENTS),
        "guard_ok": not guard_reasons,
        "updated_count": updated_count,
        "candidate_generation_count": 0,
        "apply_output_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "output_files": output_files,
    }, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
