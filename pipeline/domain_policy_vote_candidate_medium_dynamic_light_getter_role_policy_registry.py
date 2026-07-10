from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "medium_dynamic_light_getter_role_policy_registry_v1"
AGENT_KEY = "medium_dynamic_light_getter_role_policy"
AGENT_TYPE = "subcoordinator"
OPERATIONAL_STATE = "shadow"
DECISION_ROLE = "route_and_split"
PARENT_AGENT_KEY = "domain_policy_vote_candidate"
SCOPE_GROUP = "domain_policy_vote_candidate"
DASHBOARD_GROUP = "Issue Network"
NAMED_SCOPE_SUMMARY_PATH = Path("reports/20260629_140956_340120_domain_policy_vote_candidate_medium_dynamic_light_named_scope_getter_review_summary.json")
ROOT_GETTER_SUMMARY_PATH = Path("reports/20260629_143806_108361_domain_policy_vote_candidate_medium_dynamic_light_root_getter_review_summary.json")
EXPECTED_COMBINED_COUNTS = {
    "title_or_realm_name": 21,
    "faith_name": 19,
    "culture_name": 5,
    "title_base_name": 4,
    "possessive_or_relation": 9,
    "culture_collective": 6,
    "divine_realm_or_concept": 3,
    "faith_adjective": 2,
}
ROUTE_ROLES = {
    "title_or_realm_name": "route_getter_title_or_realm_name",
    "faith_name": "route_getter_faith_name",
    "culture_name": "route_getter_culture_name",
    "title_base_name": "route_getter_title_base_name",
}
HOLD_ROLES = [
    "possessive_or_relation",
    "culture_collective",
    "divine_realm_or_concept",
    "faith_adjective",
]


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def counter_from_items(items: list[dict[str, Any]]) -> Counter[str]:
    return Counter({str(item["key"]): int(item["count"]) for item in items})


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def validate_inputs(named_summary: dict[str, Any], root_summary: dict[str, Any]) -> dict[str, Any]:
    if int(named_summary.get("review_count") or 0) != 42:
        raise SystemExit("named scope review_count guard failed")
    if int(root_summary.get("review_count") or 0) != 27:
        raise SystemExit("root getter review_count guard failed")
    if named_summary.get("count_matches_expected") is not True:
        raise SystemExit("named scope count_matches_expected guard failed")
    if root_summary.get("count_matches_expected") is not True:
        raise SystemExit("root getter count_matches_expected guard failed")

    named_counts = counter_from_items(named_summary.get("grammar_role_counts") or [])
    root_counts = counter_from_items(root_summary.get("grammar_role_counts") or [])
    combined = Counter(named_counts)
    combined.update(root_counts)
    if dict(combined) != EXPECTED_COMBINED_COUNTS:
        raise SystemExit(f"combined counts guard failed: {dict(combined)} expected {EXPECTED_COMBINED_COUNTS}")

    route_count = sum(combined[role] for role in ROUTE_ROLES)
    hold_count = sum(combined[role] for role in HOLD_ROLES)
    if route_count + hold_count != sum(combined.values()):
        raise SystemExit("route+hold coverage guard failed")

    return {
        "named_scope_count": int(named_summary["review_count"]),
        "root_getter_count": int(root_summary["review_count"]),
        "combined_total": sum(combined.values()),
        "combined_counts": combined,
        "route_count": route_count,
        "hold_count": hold_count,
    }


def planned_record(validation: dict[str, Any]) -> dict[str, Any]:
    notes = {
        "source": SOURCE,
        "named_scope_summary_path": str(NAMED_SCOPE_SUMMARY_PATH),
        "root_getter_summary_path": str(ROOT_GETTER_SUMMARY_PATH),
        "agent_key": AGENT_KEY,
        "agent_type": AGENT_TYPE,
        "operational_state": OPERATIONAL_STATE,
        "decision_role": DECISION_ROLE,
        "parent_agent_key": PARENT_AGENT_KEY,
        "scope_group": SCOPE_GROUP,
        "dashboard_group": DASHBOARD_GROUP,
        "candidate_generation_allowed": False,
        "auto_apply_allowed": False,
        "lifecycle_allowed": False,
        "production_release_allowed": False,
        "segment_state_allowed": False,
        "reindex_allowed": False,
        "full_production_allowed": False,
        "read_only_component": True,
        "named_scope_count": int(validation["named_scope_count"]),
        "root_getter_count": int(validation["root_getter_count"]),
        "combined_total": int(validation["combined_total"]),
        "combined_role_counts": dict(validation["combined_counts"]),
        "route_roles": ROUTE_ROLES,
        "hold_roles": HOLD_ROLES,
        "route_count": int(validation["route_count"]),
        "hold_count": int(validation["hold_count"]),
        "guards": {
            "candidate_generation": False,
            "apply": False,
            "lifecycle": False,
            "segment_state": False,
            "reindex": False,
            "full_production": False,
        },
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
        "scope_description": (
            "Read-only unified getter-role splitter for domain_policy_vote_candidate medium_dynamic_light; "
            "routes safe name/base-name roles and holds possessive, collective, divine concept, and adjective roles."
        ),
        "dashboard_group": DASHBOARD_GROUP,
        "priority": 44,
        "notes_json": notes,
    }


def upsert_record(conn: sqlite3.Connection, record: dict[str, Any]) -> tuple[int, int]:
    now = db.utc_now()
    exists = conn.execute("SELECT 1 FROM ml_agent_registry WHERE agent_key = ?", (record["agent_key"],)).fetchone()
    created = 0 if exists else 1
    updated = 1 if exists else 0
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
    return created, updated


def validate_registry(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM ml_agent_registry WHERE agent_key = ?", (AGENT_KEY,)).fetchone()
    if row is None:
        return {"exists": False}
    payload = dict(row)
    notes = json.loads(payload.get("notes_json") or "{}")
    return {
        "exists": True,
        "agent_key": payload.get("agent_key"),
        "agent_type": payload.get("agent_type"),
        "parent_agent_key": payload.get("parent_agent_key"),
        "status": payload.get("status"),
        "operational_state": payload.get("operational_state"),
        "decision_role": payload.get("decision_role"),
        "scope_group": payload.get("scope_group"),
        "dashboard_group": payload.get("dashboard_group"),
        "candidate_generation_allowed": bool(notes.get("candidate_generation_allowed")),
        "auto_apply_allowed": bool(notes.get("auto_apply_allowed")),
        "lifecycle_allowed": bool(notes.get("lifecycle_allowed")),
        "production_release_allowed": bool(notes.get("production_release_allowed")),
        "combined_role_counts": notes.get("combined_role_counts"),
        "route_roles": notes.get("route_roles"),
        "hold_roles": notes.get("hold_roles"),
    }


def write_reports(
    *,
    apply: bool,
    record: dict[str, Any],
    validation: dict[str, Any],
    created: int,
    updated: int,
    registry_validation: dict[str, Any],
) -> tuple[Path, Path, Path]:
    mode = "apply" if apply else "dry_run"
    base = reports_dir() / f"{stamp()}_{AGENT_KEY}_registry_{mode}"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": mode,
        "agent_key": AGENT_KEY,
        "agent_type": AGENT_TYPE,
        "operational_state": OPERATIONAL_STATE,
        "decision_role": DECISION_ROLE,
        "parent_agent_key": PARENT_AGENT_KEY,
        "scope_group": SCOPE_GROUP,
        "dashboard_group": DASHBOARD_GROUP,
        "named_scope_summary_path": str(NAMED_SCOPE_SUMMARY_PATH),
        "root_getter_summary_path": str(ROOT_GETTER_SUMMARY_PATH),
        "named_scope_count": int(validation["named_scope_count"]),
        "root_getter_count": int(validation["root_getter_count"]),
        "combined_total": int(validation["combined_total"]),
        "combined_role_counts": dict(validation["combined_counts"]),
        "route_roles": ROUTE_ROLES,
        "hold_roles": HOLD_ROLES,
        "route_count": int(validation["route_count"]),
        "hold_count": int(validation["hold_count"]),
        "created": created,
        "updated": updated,
        "registry_validation": registry_validation,
        "candidate_generation_allowed": False,
        "auto_apply_allowed": False,
        "lifecycle_allowed": False,
        "production_release_allowed": False,
        "ran_candidate_generation": False,
        "ran_apply": False,
        "ran_lifecycle": False,
        "ran_segment_state": False,
        "ran_reindex": False,
        "ran_production_full": False,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "output_files": {},
    }
    with jsonl_path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps({"record_type": "registry_summary", **summary}, ensure_ascii=False, sort_keys=True) + "\n")
        handle.write(json.dumps({"record_type": "registry_record", **record}, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary_json": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "medium_dynamic_light_getter_role_policy registry",
        "",
        f"mode: {mode}",
        f"agent_key: {AGENT_KEY}",
        f"agent_type: {AGENT_TYPE}",
        f"operational_state: {OPERATIONAL_STATE}",
        f"decision_role: {DECISION_ROLE}",
        f"combined_total: {validation['combined_total']}",
        "",
        "route_roles:",
    ]
    lines.extend(f"- {role}: {route}" for role, route in ROUTE_ROLES.items())
    lines.extend(["", "hold_roles:"])
    lines.extend(f"- {role}" for role in HOLD_ROLES)
    lines.extend(["", "combined_role_counts:"])
    lines.extend(f"- {count} | {role}" for role, count in validation["combined_counts"].most_common())
    lines.extend(
        [
            "",
            "guards:",
            "- candidate_generation_allowed: false",
            "- auto_apply_allowed: false",
            "- lifecycle_allowed: false",
            "- production_release_allowed: false",
            "- apply: not_run",
            "- lifecycle: not_run",
            "- segment_state: not_run",
            "- reindex: not_run",
            "- full_production: not_run",
            "",
            f"created: {created}",
            f"updated: {updated}",
            f"registry_validation: {json.dumps(registry_validation, ensure_ascii=False, sort_keys=True)}",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    named_summary = read_json(NAMED_SCOPE_SUMMARY_PATH)
    root_summary = read_json(ROOT_GETTER_SUMMARY_PATH)
    validation = validate_inputs(named_summary, root_summary)
    record = planned_record(validation)

    created = 0
    updated = 0
    if args.apply:
        with db.connect(db.load_settings()) as conn:
            created, updated = upsert_record(conn, record)
            conn.commit()
            registry_validation = validate_registry(conn)
    else:
        with connect_readonly() as conn:
            registry_validation = validate_registry(conn)

    for key in ("candidate_generation_allowed", "auto_apply_allowed", "lifecycle_allowed", "production_release_allowed"):
        if registry_validation.get("exists") and registry_validation.get(key) is not False:
            raise SystemExit(f"registry validation failed: {key}")

    txt_path, jsonl_path, summary_path = write_reports(
        apply=args.apply,
        record=record,
        validation=validation,
        created=created,
        updated=updated,
        registry_validation=registry_validation,
    )
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"mode={'apply' if args.apply else 'dry_run'}")
    print(f"agent_key={AGENT_KEY}")
    print(f"combined_total={validation['combined_total']}")
    print("combined_role_counts=" + json.dumps(dict(validation["combined_counts"]), ensure_ascii=False, sort_keys=True))
    print(f"created={created}")
    print(f"updated={updated}")
    print("candidate_generation_allowed=false")
    print("auto_apply_allowed=false")
    print("lifecycle_allowed=false")
    print("production_release_allowed=false")
    print("ran_candidate_generation=false")
    print("ran_apply=false")
    print("ran_lifecycle=false")
    print("ran_segment_state=false")
    print("ran_reindex=false")
    print("ran_production_full=false")


if __name__ == "__main__":
    main()
