from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_religion_faith_getter_role_policy_registry_v1"
AGENT_KEY = "domain_policy_vote_candidate_religion_faith_getter_role_policy"
AGENT_TYPE = "subcoordinator"
OPERATIONAL_STATE = "shadow"
DECISION_ROLE = "route_and_split"
PARENT_AGENT_KEY = "domain_policy_vote_candidate"
SCOPE_GROUP = "domain_policy_vote_candidate"
DASHBOARD_GROUP = "Issue Network"
SUMMARY_PATH = Path("reports/20260630_082516_069795_domain_policy_vote_candidate_religion_faith_getter_role_review_summary.json")
JSONL_PATH = Path("reports/20260630_082516_069795_domain_policy_vote_candidate_religion_faith_getter_role_review.jsonl")
EXPECTED_ROLE_COUNTS = {
    "religion_name": 145,
    "faith_possessive": 20,
    "faith_adjective": 6,
    "faith_name": 6,
    "dense_structural_getter_cluster": 5,
    "article_preposition_context": 4,
    "religion_family_name": 3,
    "unknown_hold": 3,
    "faith_high_god_or_divine_name": 2,
    "faith_adherent_getter": 1,
    "faith_priest_or_clergy_getter": 1,
}
ROUTE_ROLES = {
    "faith_name": "route_faith_getter_faith_name",
    "religion_name": "route_faith_getter_religion_name",
    "religion_family_name": "route_faith_getter_religion_family_name",
    "faith_adjective": "route_faith_getter_faith_adjective",
}
HOLD_ROLES = [
    "faith_possessive",
    "dense_structural_getter_cluster",
    "article_preposition_context",
    "unknown_hold",
    "faith_high_god_or_divine_name",
    "faith_adherent_getter",
    "faith_priest_or_clergy_getter",
]


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def counter_from_items(items: list[dict[str, Any]]) -> Counter[str]:
    return Counter({str(item["key"]): int(item["count"]) for item in items})


def validate_inputs(summary: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    if summary.get("mode") != "read_only_getter_role_review":
        raise SystemExit("summary mode guard failed")
    if summary.get("lane") != SCOPE_GROUP:
        raise SystemExit("summary lane guard failed")
    if summary.get("surface_bucket") != "religion_faith_doctrine":
        raise SystemExit("summary surface_bucket guard failed")
    if summary.get("architecture_family") != "faith/doctrine getters":
        raise SystemExit("summary architecture_family guard failed")
    if int(summary.get("review_count") or 0) != 196:
        raise SystemExit("review_count guard failed")
    if summary.get("count_matches_expected") is not True:
        raise SystemExit("count_matches_expected guard failed")
    if summary.get("production_full_recommended_now") is not False:
        raise SystemExit("production_full_recommended_now guard failed")
    if summary.get("source_changed") is not False or summary.get("output_changed") is not False:
        raise SystemExit("source/output changed guard failed")
    if len(rows) != 196:
        raise SystemExit(f"jsonl row count guard failed: {len(rows)}")

    role_counts = Counter(str(row.get("getter_role") or "") for row in rows)
    if dict(role_counts) != EXPECTED_ROLE_COUNTS:
        raise SystemExit(f"role counts guard failed: {dict(role_counts)} expected {EXPECTED_ROLE_COUNTS}")
    summary_role_counts = counter_from_items(summary.get("getter_role_counts") or [])
    if dict(summary_role_counts) != EXPECTED_ROLE_COUNTS:
        raise SystemExit(f"summary role counts guard failed: {dict(summary_role_counts)}")

    duplicate_ids = [segment_id for segment_id, count in Counter(int(row["segment_id"]) for row in rows).items() if count > 1]
    if duplicate_ids:
        raise SystemExit(f"duplicate segment ids guard failed: {duplicate_ids[:10]}")

    route_count = sum(role_counts[role] for role in ROUTE_ROLES)
    hold_count = sum(role_counts[role] for role in HOLD_ROLES)
    if route_count + hold_count != len(rows):
        raise SystemExit("route+hold coverage guard failed")

    safe_route_count = sum(
        1
        for row in rows
        if row.get("role_recommendation") == "read_only_splitter_candidate"
        and row.get("getter_role") in ROUTE_ROLES
    )
    if safe_route_count != 71:
        raise SystemExit(f"safe route count guard failed: {safe_route_count}")

    return {
        "review_count": len(rows),
        "role_counts": role_counts,
        "route_count": route_count,
        "hold_count": hold_count,
        "safe_route_count": safe_route_count,
        "segment_ids": sorted(int(row["segment_id"]) for row in rows),
    }


def planned_record(validation: dict[str, Any]) -> dict[str, Any]:
    notes = {
        "source": SOURCE,
        "summary_path": str(SUMMARY_PATH),
        "jsonl_path": str(JSONL_PATH),
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
        "review_count": int(validation["review_count"]),
        "role_counts": dict(validation["role_counts"]),
        "route_roles": ROUTE_ROLES,
        "hold_roles": HOLD_ROLES,
        "route_count": int(validation["route_count"]),
        "hold_count": int(validation["hold_count"]),
        "safe_route_count": int(validation["safe_route_count"]),
        "policy_recommendation": (
            "Shadow faith/religion getter role splitter only. Route faith_name, religion_name, "
            "religion_family_name, and faith_adjective for later read-only subreviews; hold possessive, "
            "divine runtime, clergy/adherent, article/preposition, dense, unknown, and multiline contexts."
        ),
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
            "Read-only shadow splitter for religion_faith_doctrine faith/doctrine getter roles inside "
            "domain_policy_vote_candidate."
        ),
        "dashboard_group": DASHBOARD_GROUP,
        "priority": 42,
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
        "route_roles": notes.get("route_roles"),
        "hold_roles": notes.get("hold_roles"),
        "safe_route_count": notes.get("safe_route_count"),
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
        "summary_path": str(SUMMARY_PATH),
        "jsonl_path": str(JSONL_PATH),
        "review_count": int(validation["review_count"]),
        "role_counts": dict(validation["role_counts"]),
        "route_roles": ROUTE_ROLES,
        "hold_roles": HOLD_ROLES,
        "route_count": int(validation["route_count"]),
        "hold_count": int(validation["hold_count"]),
        "safe_route_count": int(validation["safe_route_count"]),
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
        "domain_policy_vote_candidate religion faith getter role policy registry",
        "",
        f"mode: {mode}",
        f"agent_key: {AGENT_KEY}",
        f"agent_type: {AGENT_TYPE}",
        f"operational_state: {OPERATIONAL_STATE}",
        f"decision_role: {DECISION_ROLE}",
        f"review_count: {validation['review_count']}",
        f"safe_route_count: {validation['safe_route_count']}",
        "",
        "route_roles:",
    ]
    lines.extend(f"- {role}: {route}" for role, route in ROUTE_ROLES.items())
    lines.extend(["", "hold_roles:"])
    lines.extend(f"- {role}" for role in HOLD_ROLES)
    lines.extend(["", "role_counts:"])
    lines.extend(f"- {count} | {role}" for role, count in validation["role_counts"].most_common())
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

    summary = read_json(SUMMARY_PATH)
    rows = read_jsonl(JSONL_PATH)
    validation = validate_inputs(summary, rows)
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
    print(f"review_count={validation['review_count']}")
    print(f"safe_route_count={validation['safe_route_count']}")
    print("role_counts=" + json.dumps(dict(validation["role_counts"]), ensure_ascii=False, sort_keys=True))
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
