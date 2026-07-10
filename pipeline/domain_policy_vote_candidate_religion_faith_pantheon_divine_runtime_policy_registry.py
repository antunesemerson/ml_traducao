from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_religion_faith_pantheon_divine_runtime_policy_registry_v1"
AGENT_KEY = "religion_faith_doctrine_pantheon_divine_runtime_policy"
AGENT_TYPE = "subcoordinator"
OPERATIONAL_STATE = "shadow"
DECISION_ROLE = "route_and_split"
PARENT_AGENT_KEY = "domain_policy_vote_candidate"
SCOPE_GROUP = "domain_policy_vote_candidate"
DASHBOARD_GROUP = "Issue Network"
REVIEW_SUMMARY_PATH = Path("reports/20260630_115439_935177_domain_policy_vote_candidate_religion_faith_pantheon_divine_runtime_review_summary.json")
REVIEW_JSONL_PATH = Path("reports/20260630_115439_935177_domain_policy_vote_candidate_religion_faith_pantheon_divine_runtime_review.jsonl")
EXPECTED_REVIEW_COUNT = 10
EXPECTED_COUNTS = {
    "high_god_possessive_runtime": 8,
    "legend_pantheon_religious_text_runtime": 1,
    "residual_divine_runtime_context": 1,
}


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
        sibling_count = int(conn.execute("SELECT COUNT(*) FROM ml_agent_registry WHERE parent_agent_key = ? AND scope_group = ?", (PARENT_AGENT_KEY, SCOPE_GROUP)).fetchone()[0] or 0)
        if sibling_count <= 0:
            raise SystemExit("missing logical parent")
        return {"exists": False, "logical_parent": True, "agent_key": PARENT_AGENT_KEY, "sibling_count": sibling_count}
    return {"exists": True, "logical_parent": False, "agent_key": row["agent_key"], "status": row["status"]}


def choose_priority(conn: sqlite3.Connection) -> tuple[int, str]:
    used = {int(row["priority"]) for row in conn.execute("SELECT priority FROM ml_agent_registry WHERE priority IS NOT NULL").fetchall() if row["priority"] is not None}
    priority = 48
    while priority in used:
        priority += 1
    return priority, "priority_48_available" if priority == 48 else f"priority_48_conflict_used_{priority}"


def validate_inputs(summary: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    if summary.get("mode") != "read_only_review":
        raise SystemExit("review mode guard failed")
    if int(summary.get("review_count") or 0) != EXPECTED_REVIEW_COUNT or len(rows) != EXPECTED_REVIEW_COUNT:
        raise SystemExit("review_count guard failed")
    if summary.get("count_matches_expected") is not True:
        raise SystemExit("count guard failed")
    if summary.get("source_changed") is not False or summary.get("output_changed") is not False:
        raise SystemExit("source/output guard failed")
    subtype_counts = Counter(str(row.get("operational_subtype") or "") for row in rows)
    if dict(subtype_counts) != EXPECTED_COUNTS:
        raise SystemExit(f"subtype count guard failed: {dict(subtype_counts)}")
    return {
        "review_count": len(rows),
        "subtype_counts": subtype_counts,
        "action_counts": Counter(str(row.get("recommended_action") or "") for row in rows),
        "segment_ids_by_subtype": {
            subtype: [int(row["segment_id"]) for row in rows if row.get("operational_subtype") == subtype]
            for subtype in sorted(subtype_counts)
        },
    }


def planned_record(validation: dict[str, Any], parent_validation: dict[str, Any], priority: int, priority_note: str) -> dict[str, Any]:
    notes = {
        "source": SOURCE,
        "review_summary_path": str(REVIEW_SUMMARY_PATH),
        "review_jsonl_path": str(REVIEW_JSONL_PATH),
        "agent_key": AGENT_KEY,
        "agent_type": AGENT_TYPE,
        "operational_state": OPERATIONAL_STATE,
        "decision_role": DECISION_ROLE,
        "parent_agent_key": PARENT_AGENT_KEY,
        "parent_validation": parent_validation,
        "scope_group": SCOPE_GROUP,
        "dashboard_group": DASHBOARD_GROUP,
        "surface_bucket": "religion_faith_doctrine",
        "hold_family": "pantheon_divine_runtime_name",
        "candidate_generation_allowed": False,
        "auto_apply_allowed": False,
        "lifecycle_allowed": False,
        "production_release_allowed": False,
        "segment_state_allowed": False,
        "reindex_allowed": False,
        "full_production_allowed": False,
        "read_only_component": True,
        "does_not_close_segments": True,
        "review_count": int(validation["review_count"]),
        "subtype_counts": dict(validation["subtype_counts"]),
        "action_counts": dict(validation["action_counts"]),
        "route_subtypes": {"high_god_possessive_runtime": "route_divine_possessive_parser_review"},
        "hold_subtypes": {
            "legend_pantheon_religious_text_runtime": "hold_legend_runtime_context",
            "residual_divine_runtime_context": "hold_residual_divine_runtime_context",
        },
        "segment_ids_by_subtype": validation["segment_ids_by_subtype"],
        "policy_recommendation": "Read-only splitter for divine runtime surfaces; only route HighGodNamePossessive parser review.",
        "guards": {"candidate_generation": False, "apply": False, "lifecycle": False, "segment_state": False, "reindex": False, "full_production": False},
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
        "scope_description": "Read-only splitter for religion_faith_doctrine pantheon/divine runtime surfaces.",
        "dashboard_group": DASHBOARD_GROUP,
        "priority": priority,
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
    notes = json.loads(row["notes_json"] or "{}")
    return {
        "exists": True,
        "agent_key": row["agent_key"],
        "agent_type": row["agent_type"],
        "operational_state": row["operational_state"],
        "decision_role": row["decision_role"],
        "candidate_generation_allowed": bool(notes.get("candidate_generation_allowed")),
        "auto_apply_allowed": bool(notes.get("auto_apply_allowed")),
        "lifecycle_allowed": bool(notes.get("lifecycle_allowed")),
        "production_release_allowed": bool(notes.get("production_release_allowed")),
    }


def write_reports(apply: bool, record: dict[str, Any], validation: dict[str, Any], created: int, updated: int, registry_validation: dict[str, Any]) -> Path:
    mode = "apply" if apply else "dry_run"
    base = reports_dir() / f"{stamp()}_{AGENT_KEY}_registry_{mode}"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    result = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": mode,
        "agent_key": AGENT_KEY,
        "agent_type": AGENT_TYPE,
        "operational_state": OPERATIONAL_STATE,
        "decision_role": DECISION_ROLE,
        "review_count": int(validation["review_count"]),
        "subtype_counts": dict(validation["subtype_counts"]),
        "action_counts": dict(validation["action_counts"]),
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
        "output_files": {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary_json": str(summary_path)},
    }
    with jsonl_path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps({"record_type": "registry_summary", **result}, ensure_ascii=False, sort_keys=True) + "\n")
        handle.write(json.dumps({"record_type": "registry_record", **record}, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    txt_path.write_text(f"mode: {mode}\nagent_key: {AGENT_KEY}\nreview_count: {validation['review_count']}\n", encoding="utf-8")
    return summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    validation = validate_inputs(read_json(REVIEW_SUMMARY_PATH), read_jsonl(REVIEW_JSONL_PATH))
    created = updated = 0
    if args.apply:
        with db.connect(db.load_settings()) as conn:
            parent = validate_parent(conn)
            priority, note = choose_priority(conn)
            record = planned_record(validation, parent, priority, note)
            created, updated = upsert_record(conn, record)
            conn.commit()
            reg = validate_registry(conn)
    else:
        with connect_readonly() as conn:
            parent = validate_parent(conn)
            priority, note = choose_priority(conn)
            record = planned_record(validation, parent, priority, note)
            reg = validate_registry(conn)
    if reg.get("exists"):
        for key in ("candidate_generation_allowed", "auto_apply_allowed", "lifecycle_allowed", "production_release_allowed"):
            if reg.get(key) is not False:
                raise SystemExit(f"registry validation failed: {key}")
    summary_path = write_reports(args.apply, record, validation, created, updated, reg)
    print(f"summary={summary_path}")
    print(f"mode={'apply' if args.apply else 'dry_run'}")
    print(f"agent_key={AGENT_KEY}")
    print(f"review_count={validation['review_count']}")
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
