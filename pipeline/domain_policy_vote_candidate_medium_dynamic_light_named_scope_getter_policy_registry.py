from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "medium_dynamic_light_named_scope_getter_policy_registry_v1"
AGENT_KEY = "medium_dynamic_light_named_scope_getter_policy"
AGENT_TYPE = "subcoordinator"
OPERATIONAL_STATE = "shadow"
DECISION_ROLE = "route_and_split"
PARENT_AGENT_KEY = "domain_policy_vote_candidate"
SCOPE_GROUP = "domain_policy_vote_candidate"
DASHBOARD_GROUP = "Issue Network"
REVIEW_PATH = Path("reports/20260629_140956_340120_domain_policy_vote_candidate_medium_dynamic_light_named_scope_getter_review.jsonl")
SUMMARY_PATH = Path("reports/20260629_140956_340120_domain_policy_vote_candidate_medium_dynamic_light_named_scope_getter_review_summary.json")
EXPECTED_TOTAL = 42
EXPECTED_ROLES = {
    "faith_name": 16,
    "title_or_realm_name": 14,
    "culture_name": 3,
    "possessive_or_relation": 9,
}
HOLD_ROLES = ["possessive_or_relation"]


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


def validate_inputs(rows: list[dict[str, Any]], summary: dict[str, Any]) -> dict[str, Any]:
    if int(summary.get("review_count") or 0) != EXPECTED_TOTAL:
        raise SystemExit(f"summary review_count guard failed: {summary.get('review_count')}")
    if summary.get("count_matches_expected") is not True:
        raise SystemExit("summary count_matches_expected guard failed")
    if len(rows) != EXPECTED_TOTAL:
        raise SystemExit(f"review row count guard failed: {len(rows)}")
    if any(row.get("architecture_family") != "named_scope_getters" for row in rows):
        raise SystemExit("architecture_family guard failed")

    role_counts = Counter(str(row.get("grammar_role") or "") for row in rows)
    if dict(role_counts) != EXPECTED_ROLES:
        raise SystemExit(f"role counts guard failed: {dict(role_counts)} expected {EXPECTED_ROLES}")

    ids = [int(row["segment_id"]) for row in rows]
    if len(ids) != len(set(ids)):
        raise SystemExit("duplicate segment_id guard failed")

    return {
        "review_total": len(rows),
        "role_counts": role_counts,
        "hold_count": sum(role_counts[role] for role in HOLD_ROLES),
        "splitter_candidate_count": sum(count for role, count in role_counts.items() if role not in HOLD_ROLES),
        "segment_ids": sorted(ids),
    }


def planned_record(validation: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now().isoformat(timespec="seconds")
    notes = {
        "source": SOURCE,
        "review_path": str(REVIEW_PATH),
        "summary_path": str(SUMMARY_PATH),
        "agent_key": AGENT_KEY,
        "agent_type": AGENT_TYPE,
        "operational_state": OPERATIONAL_STATE,
        "decision_role": DECISION_ROLE,
        "parent_agent_key": PARENT_AGENT_KEY,
        "scope_group": SCOPE_GROUP,
        "dashboard_group": DASHBOARD_GROUP,
        "production_release_allowed": False,
        "auto_apply_allowed": False,
        "lifecycle_allowed": False,
        "candidate_generation_allowed": False,
        "reindex_allowed": False,
        "segment_state_allowed": False,
        "full_production_allowed": False,
        "read_only_component": True,
        "operational_hold": True,
        "review_total": int(validation["review_total"]),
        "role_counts": dict(validation["role_counts"]),
        "splitter_candidate_roles": ["faith_name", "title_or_realm_name", "culture_name"],
        "hold_roles": HOLD_ROLES,
        "hold_count": int(validation["hold_count"]),
        "splitter_candidate_count": int(validation["splitter_candidate_count"]),
        "policy_recommendation": (
            "Shadow route-and-split policy only. Split named-scope getters by grammar role; keep "
            "possessive_or_relation in hold/context until parser-backed grammar policy exists."
        ),
        "guards": {
            "generate_candidates": False,
            "apply": False,
            "lifecycle": False,
            "segment_state": False,
            "reindex": False,
            "full_production": False,
        },
        "created_by": SOURCE,
        "registered_at": now,
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
            "Read-only shadow splitter for domain_policy_vote_candidate medium_dynamic_light named-scope getters; "
            "routes faith/title/culture name roles separately and holds possessive/relation roles."
        ),
        "dashboard_group": DASHBOARD_GROUP,
        "priority": 45,
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
            agent_key,
            agent_type,
            parent_agent_key,
            model_kind,
            status,
            operational_state,
            decision_role,
            scope_group,
            scope_sql,
            scope_description,
            default_threshold,
            priority,
            dashboard_group,
            created_at,
            updated_at,
            notes_json
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
        "production_release_allowed": bool(notes.get("production_release_allowed")),
        "auto_apply_allowed": bool(notes.get("auto_apply_allowed")),
        "lifecycle_allowed": bool(notes.get("lifecycle_allowed")),
        "candidate_generation_allowed": bool(notes.get("candidate_generation_allowed")),
        "hold_roles": notes.get("hold_roles"),
        "review_total": int(notes.get("review_total") or 0),
        "role_counts": notes.get("role_counts"),
    }


def write_reports(
    *,
    apply: bool,
    record: dict[str, Any],
    validation: dict[str, Any],
    created: int,
    updated: int,
    registry_validation: dict[str, Any] | None,
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
        "review_path": str(REVIEW_PATH),
        "summary_path": str(SUMMARY_PATH),
        "review_total": int(validation["review_total"]),
        "role_counts": dict(validation["role_counts"]),
        "hold_roles": HOLD_ROLES,
        "hold_count": int(validation["hold_count"]),
        "splitter_candidate_count": int(validation["splitter_candidate_count"]),
        "created": created,
        "updated": updated,
        "registry_validation": registry_validation or {},
        "production_release_allowed": False,
        "auto_apply_allowed": False,
        "lifecycle_allowed": False,
        "candidate_generation_allowed": False,
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
    summary["output_files"] = {
        "txt": str(txt_path),
        "jsonl": str(jsonl_path),
        "summary_json": str(summary_path),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "medium_dynamic_light_named_scope_getter_policy registry",
        "",
        f"mode: {mode}",
        f"agent_key: {AGENT_KEY}",
        f"agent_type: {AGENT_TYPE}",
        f"operational_state: {OPERATIONAL_STATE}",
        f"decision_role: {DECISION_ROLE}",
        f"parent_agent_key: {PARENT_AGENT_KEY}",
        f"scope_group: {SCOPE_GROUP}",
        f"dashboard_group: {DASHBOARD_GROUP}",
        f"review_total: {validation['review_total']}",
        "",
        "role_counts:",
    ]
    lines.extend(f"- {count} | {role}" for role, count in validation["role_counts"].most_common())
    lines.extend(
        [
            "",
            "guards:",
            "- production_release_allowed: false",
            "- auto_apply_allowed: false",
            "- lifecycle_allowed: false",
            "- candidate_generation_allowed: false",
            "- segment_state: not_run",
            "- reindex: not_run",
            "- full_production: not_run",
            "- possessive_or_relation: hold_context",
            "",
            f"created: {created}",
            f"updated: {updated}",
        ]
    )
    if registry_validation:
        lines.append(f"registry_validation: {json.dumps(registry_validation, ensure_ascii=False, sort_keys=True)}")
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    rows = read_jsonl(REVIEW_PATH)
    summary = read_json(SUMMARY_PATH)
    validation = validate_inputs(rows, summary)
    record = planned_record(validation)

    created = 0
    updated = 0
    registry_validation = None
    if args.apply:
        with db.connect(db.load_settings()) as conn:
            created, updated = upsert_record(conn, record)
            conn.commit()
            registry_validation = validate_registry(conn)
            if registry_validation.get("production_release_allowed") is not False:
                raise SystemExit("registry validation failed: production_release_allowed")
            if registry_validation.get("auto_apply_allowed") is not False:
                raise SystemExit("registry validation failed: auto_apply_allowed")
            if registry_validation.get("lifecycle_allowed") is not False:
                raise SystemExit("registry validation failed: lifecycle_allowed")
    else:
        with connect_readonly() as conn:
            registry_validation = validate_registry(conn)

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
    print(f"review_total={validation['review_total']}")
    print("role_counts=" + json.dumps(dict(validation["role_counts"]), ensure_ascii=False, sort_keys=True))
    print(f"created={created}")
    print(f"updated={updated}")
    print("production_release_allowed=false")
    print("auto_apply_allowed=false")
    print("lifecycle_allowed=false")
    print("candidate_generation_allowed=false")
    print("ran_candidate_generation=false")
    print("ran_apply=false")
    print("ran_lifecycle=false")
    print("ran_segment_state=false")
    print("ran_reindex=false")
    print("ran_production_full=false")


if __name__ == "__main__":
    main()
