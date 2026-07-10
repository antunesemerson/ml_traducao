from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_religion_faith_holy_site_effect_name_policy_registry_v1"
AGENT_KEY = "religion_faith_doctrine_holy_site_effect_name_preposition_policy"
AGENT_TYPE = "subcoordinator"
OPERATIONAL_STATE = "shadow"
DECISION_ROLE = "route_and_split"
PARENT_AGENT_KEY = "religion_faith_doctrine_holy_site_effect_requirement_splitter_policy"
SCOPE_GROUP = "domain_policy_vote_candidate"
DASHBOARD_GROUP = "Issue Network"
DIAGNOSTIC_SUMMARY_PATH = Path(
    "reports/20260630_132349_724412_domain_policy_vote_candidate_religion_faith_holy_site_dense_token_diagnostic_summary.json"
)
DIAGNOSTIC_JSONL_PATH = Path(
    "reports/20260630_132349_724412_domain_policy_vote_candidate_religion_faith_holy_site_dense_token_diagnostic.jsonl"
)
EXPECTED_SEGMENT_STATE_RUN_ID = 509
EXPECTED_ROUTE_COUNT = 73
EXPECTED_PARSER_LATER_COUNT = 4
EXPECTED_SMALL_HUMAN_COUNT = 1


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def validate_parent(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM ml_agent_registry WHERE agent_key = ?", (PARENT_AGENT_KEY,)).fetchone()
    if row is None:
        raise SystemExit(f"missing parent agent registry record: {PARENT_AGENT_KEY}")
    payload = dict(row)
    notes = json.loads(payload.get("notes_json") or "{}")
    if payload.get("operational_state") != "shadow":
        raise SystemExit("parent operational_state guard failed")
    if notes.get("candidate_generation_allowed") is not False:
        raise SystemExit("parent candidate_generation guard failed")
    if notes.get("auto_apply_allowed") is not False:
        raise SystemExit("parent auto_apply guard failed")
    if notes.get("lifecycle_allowed") is not False:
        raise SystemExit("parent lifecycle guard failed")
    return {
        "exists": True,
        "agent_key": payload.get("agent_key"),
        "agent_type": payload.get("agent_type"),
        "operational_state": payload.get("operational_state"),
        "decision_role": payload.get("decision_role"),
        "scope_group": payload.get("scope_group"),
    }


def choose_priority(conn: sqlite3.Connection) -> tuple[int, str]:
    used = {
        int(row["priority"])
        for row in conn.execute("SELECT priority FROM ml_agent_registry WHERE priority IS NOT NULL")
        if row["priority"] is not None
    }
    priority = 45
    while priority in used:
        priority += 1
    note = "priority_45_available" if priority == 45 else f"priority_45_conflict_used_{priority}"
    return priority, note


def validate_inputs(summary: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    if summary.get("mode") != "read_only_dense_token_diagnostic":
        raise SystemExit("diagnostic mode guard failed")
    if int(summary.get("segment_state_run_id") or 0) != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("segment_state_run_id guard failed")
    if int(summary.get("diagnostic_count") or 0) != 78 or len(rows) != 78:
        raise SystemExit("diagnostic count guard failed")
    if summary.get("count_matches_expected") is not True:
        raise SystemExit("count_matches_expected guard failed")
    for key in ("candidate_generation_count", "apply_count", "lifecycle_count", "segment_state_count", "reindex_count", "production_full_count"):
        if int(summary.get(key) or 0) != 0:
            raise SystemExit(f"{key} guard failed")
    if summary.get("source_changed") is not False or summary.get("output_changed") is not False:
        raise SystemExit("source/output changed guard failed")
    if summary.get("production_full_recommended_now") is not False:
        raise SystemExit("production full guard failed")

    family_counts = Counter(str(row.get("dense_subfamily") or "") for row in rows)
    action_counts = Counter(str(row.get("recommended_action") or "") for row in rows)
    risk_counts = Counter(str(row.get("risk_bucket") or "") for row in rows)
    route_rows = [
        row
        for row in rows
        if row.get("dense_subfamily") == "holy_site_effect_name_preposition_line"
        and row.get("recommended_action") == "subpolicy_readonly_or_tiny_human_packet"
    ]
    if len(route_rows) != EXPECTED_ROUTE_COUNT:
        raise SystemExit(f"effect-name route count guard failed: {len(route_rows)}")
    if action_counts.get("parser_later_count_and_possessive", 0) + action_counts.get("parser_later_requirement_trigger", 0) != EXPECTED_PARSER_LATER_COUNT:
        raise SystemExit("parser-later count guard failed")
    if action_counts.get("small_human_review_possible", 0) != EXPECTED_SMALL_HUMAN_COUNT:
        raise SystemExit("small-human count guard failed")
    bad_flags = [
        int(row["segment_id"])
        for row in rows
        if int(row.get("latest_needs_output_apply") or 0) != 0
        or row.get("candidate_generation_allowed") is not False
        or row.get("auto_apply_allowed") is not False
        or row.get("lifecycle_allowed") is not False
        or row.get("production_release_allowed") is not False
    ]
    if bad_flags:
        raise SystemExit(f"row guard flags failed: {bad_flags[:10]}")
    return {
        "route_rows": route_rows,
        "route_segment_ids": [int(row["segment_id"]) for row in route_rows],
        "family_counts": family_counts,
        "action_counts": action_counts,
        "risk_counts": risk_counts,
    }


def planned_record(validation: dict[str, Any], parent_validation: dict[str, Any], priority: int, priority_note: str) -> dict[str, Any]:
    notes = {
        "source": SOURCE,
        "diagnostic_summary_path": str(DIAGNOSTIC_SUMMARY_PATH),
        "diagnostic_jsonl_path": str(DIAGNOSTIC_JSONL_PATH),
        "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
        "agent_key": AGENT_KEY,
        "agent_type": AGENT_TYPE,
        "operational_state": OPERATIONAL_STATE,
        "decision_role": DECISION_ROLE,
        "parent_agent_key": PARENT_AGENT_KEY,
        "parent_validation": parent_validation,
        "scope_group": SCOPE_GROUP,
        "dashboard_group": DASHBOARD_GROUP,
        "surface_bucket": "religion_faith_doctrine",
        "hold_family": "holy_site_effect_or_requirement",
        "dense_subfamily": "holy_site_effect_name_preposition_line",
        "route": "route_holy_site_effect_name_preposition_line",
        "route_count": EXPECTED_ROUTE_COUNT,
        "route_segment_ids": validation["route_segment_ids"],
        "candidate_generation_allowed": False,
        "auto_apply_allowed": False,
        "lifecycle_allowed": False,
        "production_release_allowed": False,
        "segment_state_allowed": False,
        "reindex_allowed": False,
        "full_production_allowed": False,
        "read_only_component": True,
        "terminal_policy": False,
        "route_and_split": True,
        "does_not_close_segments": True,
        "blocked_routes": {
            "parser_later_count_and_possessive": 2,
            "parser_later_requirement_trigger": 2,
            "small_human_review_possible": 1,
        },
        "family_counts": dict(validation["family_counts"]),
        "recommended_action_counts": dict(validation["action_counts"]),
        "risk_bucket_counts": dict(validation["risk_counts"]),
        "policy_recommendation": (
            "Use as read-only splitter for repetitive holy_site_*_effect_name preposition lines. "
            "Next step should be a tiny human review or abstract parser diagnostic before any candidate generation."
        ),
        "guards": {
            "candidate_generation": False,
            "apply": False,
            "lifecycle": False,
            "segment_state": False,
            "reindex": False,
            "full_production": False,
        },
        "priority_conflict_resolution": priority_note,
        "registered_at": datetime.now().isoformat(timespec="seconds"),
    }
    return {
        "agent_key": AGENT_KEY,
        "agent_type": AGENT_TYPE,
        "parent_agent_key": PARENT_AGENT_KEY,
        "status": "active",
        "operational_state": OPERATIONAL_STATE,
        "decision_role": DECISION_ROLE,
        "scope_group": SCOPE_GROUP,
        "scope_description": "Read-only splitter for holy_site_*_effect_name preposition lines.",
        "dashboard_group": DASHBOARD_GROUP,
        "priority": priority,
        "notes_json": notes,
    }


def upsert_record(conn: sqlite3.Connection, record: dict[str, Any]) -> tuple[int, int]:
    now = db.utc_now()
    exists = conn.execute("SELECT 1 FROM ml_agent_registry WHERE agent_key = ?", (record["agent_key"],)).fetchone()
    conn.execute(
        """
        INSERT INTO ml_agent_registry (
            agent_key, agent_type, parent_agent_key, model_kind, status,
            operational_state, decision_role, scope_group, scope_sql,
            scope_description, default_threshold, priority, dashboard_group,
            created_at, updated_at, notes_json
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
            record["agent_key"],
            record["agent_type"],
            record["parent_agent_key"],
            record["status"],
            record["operational_state"],
            record["decision_role"],
            record["scope_group"],
            record["scope_description"],
            record["priority"],
            record["dashboard_group"],
            now,
            now,
            json.dumps(record["notes_json"], ensure_ascii=False, sort_keys=True),
        ),
    )
    return (0 if exists else 1, 1 if exists else 0)


def write_outputs(*, apply: bool, record: dict[str, Any], validation: dict[str, Any], parent_validation: dict[str, Any], registry_result: dict[str, Any], created: int, updated: int) -> None:
    mode = "apply" if apply else "dry_run"
    base = reports_dir() / f"{stamp()}_religion_faith_doctrine_holy_site_effect_name_preposition_policy_registry_{mode}"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": mode,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "agent_key": AGENT_KEY,
        "agent_type": AGENT_TYPE,
        "operational_state": OPERATIONAL_STATE,
        "decision_role": DECISION_ROLE,
        "parent_agent_key": PARENT_AGENT_KEY,
        "parent_validation": parent_validation,
        "registry_created_count": created,
        "registry_updated_count": updated,
        "registry_result": registry_result,
        "route_count": EXPECTED_ROUTE_COUNT,
        "family_counts": dict(validation["family_counts"]),
        "recommended_action_counts": dict(validation["action_counts"]),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "next_recommendation": "run read-only dry-run routing for the 73 holy_site effect-name preposition lines",
        "output_files": {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary_json": str(summary_path)},
    }
    with jsonl_path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "religion_faith_doctrine holy_site effect-name preposition policy registry",
        "",
        f"mode: {mode}",
        f"agent_key: {AGENT_KEY}",
        f"route_count: {EXPECTED_ROUTE_COUNT}",
        f"registry_created_count: {created}",
        f"registry_updated_count: {updated}",
        "family_counts:",
        *[f"- {count} | {key}" for key, count in validation["family_counts"].most_common()],
        "",
        "guards:",
        "- candidate_generation_count: 0",
        "- apply_count: 0",
        "- lifecycle_count: 0",
        "- segment_state_count: 0",
        "- reindex_count: 0",
        "- production_full_count: 0",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    summary = read_json(DIAGNOSTIC_SUMMARY_PATH)
    rows = read_jsonl(DIAGNOSTIC_JSONL_PATH)
    with connect_readonly() as conn:
        parent_validation = validate_parent(conn)
        priority, priority_note = choose_priority(conn)
    validation = validate_inputs(summary, rows)
    record = planned_record(validation, parent_validation, priority, priority_note)

    created = 0
    updated = 0
    registry_result: dict[str, Any] = {"exists": False, "mode": "dry_run"}
    if args.apply:
        with db.connect(db.load_settings()) as conn:
            db.ensure_database(conn)
            created, updated = upsert_record(conn, record)
            stored = conn.execute("SELECT * FROM ml_agent_registry WHERE agent_key = ?", (AGENT_KEY,)).fetchone()
            if stored is None:
                raise SystemExit("registry apply failed")
            conn.commit()
            registry_result = {
                "exists": True,
                "agent_key": stored["agent_key"],
                "operational_state": stored["operational_state"],
                "decision_role": stored["decision_role"],
            }

    write_outputs(
        apply=args.apply,
        record=record,
        validation=validation,
        parent_validation=parent_validation,
        registry_result=registry_result,
        created=created,
        updated=updated,
    )


if __name__ == "__main__":
    main()
