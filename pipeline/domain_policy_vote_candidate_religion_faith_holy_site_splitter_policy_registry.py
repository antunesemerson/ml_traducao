from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_religion_faith_holy_site_splitter_policy_registry_v1"
AGENT_KEY = "religion_faith_doctrine_holy_site_effect_requirement_splitter_policy"
AGENT_TYPE = "subcoordinator"
OPERATIONAL_STATE = "shadow"
DECISION_ROLE = "route_and_split"
PARENT_AGENT_KEY = "domain_policy_vote_candidate"
SCOPE_GROUP = "domain_policy_vote_candidate"
DASHBOARD_GROUP = "Issue Network"
REVIEW_SUMMARY_PATH = Path(
    "reports/20260630_124634_175870_domain_policy_vote_candidate_religion_faith_holy_site_effect_requirement_review_summary.json"
)
REVIEW_JSONL_PATH = Path(
    "reports/20260630_124634_175870_domain_policy_vote_candidate_religion_faith_holy_site_effect_requirement_review.jsonl"
)
EXPECTED_SEGMENT_STATE_RUN_ID = 509
EXPECTED_REVIEW_COUNT = 92
EXPECTED_EFFECT_REQUIREMENT_COUNT = 7
EXPECTED_NAME_RUNTIME_COUNT = 4
EXPECTED_ALLOWED_ROUTE_COUNT = EXPECTED_EFFECT_REQUIREMENT_COUNT + EXPECTED_NAME_RUNTIME_COUNT
EXPECTED_DENSE_HOLD_COUNT = 78


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
        sibling_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM ml_agent_registry WHERE parent_agent_key = ? AND scope_group = ?",
                (PARENT_AGENT_KEY, SCOPE_GROUP),
            ).fetchone()[0]
            or 0
        )
        if sibling_count <= 0:
            raise SystemExit(f"missing parent agent registry record and no logical-parent siblings: {PARENT_AGENT_KEY}")
        return {"exists": False, "logical_parent": True, "sibling_count": sibling_count}
    payload = dict(row)
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
    priority = 44
    while priority in used:
        priority += 1
    note = "priority_44_available" if priority == 44 else f"priority_44_conflict_used_{priority}"
    return priority, note


def route_for_subfamily(subfamily: str) -> str:
    if subfamily == "holy_site_effect_requirement":
        return "route_holy_site_effect_requirement"
    if subfamily == "holy_site_name_runtime":
        return "route_holy_site_name_runtime"
    if subfamily == "dense_token_cluster_hold":
        return "hold_dense_token_cluster"
    if subfamily == "effect_list_or_requirement_multiline":
        return "hold_effect_list_or_requirement_multiline"
    if subfamily == "building_or_place_holy_site":
        return "hold_building_or_place_holy_site"
    if subfamily == "faith_holy_site_relation":
        return "hold_faith_holy_site_relation"
    if subfamily == "title_or_realm_holy_site_context":
        return "hold_title_or_realm_holy_site_context"
    return "hold_human_context_needed"


def validate_inputs(summary: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    if summary.get("mode") != "read_only_review":
        raise SystemExit("review mode guard failed")
    if int(summary.get("latest_segment_state_run_id") or 0) != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("latest segment_state_run_id guard failed")
    if int(summary.get("review_count") or 0) != EXPECTED_REVIEW_COUNT or len(rows) != EXPECTED_REVIEW_COUNT:
        raise SystemExit("review_count guard failed")
    if summary.get("count_matches_expected") is not True:
        raise SystemExit("count_matches_expected guard failed")
    if int(summary.get("candidate_generation_count") or 0) != 0:
        raise SystemExit("candidate_generation_count guard failed")
    if int(summary.get("apply_count") or 0) != 0:
        raise SystemExit("apply_count guard failed")
    if int(summary.get("lifecycle_count") or 0) != 0:
        raise SystemExit("lifecycle_count guard failed")
    if summary.get("source_changed") is not False or summary.get("output_changed") is not False:
        raise SystemExit("source/output changed guard failed")
    if summary.get("production_full_recommended_now") is not False:
        raise SystemExit("production full guard failed")

    subfamily_counts = Counter(str(row.get("operational_subfamily") or "") for row in rows)
    if subfamily_counts.get("holy_site_effect_requirement", 0) != EXPECTED_EFFECT_REQUIREMENT_COUNT:
        raise SystemExit("holy_site_effect_requirement count guard failed")
    if subfamily_counts.get("holy_site_name_runtime", 0) != EXPECTED_NAME_RUNTIME_COUNT:
        raise SystemExit("holy_site_name_runtime count guard failed")
    if subfamily_counts.get("dense_token_cluster_hold", 0) != EXPECTED_DENSE_HOLD_COUNT:
        raise SystemExit("dense_token_cluster_hold count guard failed")

    allowed = [
        row for row in rows
        if row.get("operational_subfamily") in {"holy_site_effect_requirement", "holy_site_name_runtime"}
    ]
    if len(allowed) != EXPECTED_ALLOWED_ROUTE_COUNT:
        raise SystemExit("allowed route count guard failed")

    bad_flags = [
        int(row["segment_id"])
        for row in rows
        if row.get("candidate_generation_allowed") is not False
        or row.get("auto_apply_allowed") is not False
        or row.get("lifecycle_allowed") is not False
        or row.get("production_release_allowed") is not False
        or int(row.get("latest_needs_output_apply") or 0) != 0
    ]
    if bad_flags:
        raise SystemExit(f"row guard flags failed: {bad_flags[:10]}")

    route_counts = Counter(route_for_subfamily(str(row.get("operational_subfamily") or "")) for row in rows)
    return {
        "allowed_rows": allowed,
        "allowed_segment_ids": [int(row["segment_id"]) for row in allowed],
        "route_counts": route_counts,
        "subfamily_counts": subfamily_counts,
        "risk_bucket_counts": Counter(str(row.get("risk_bucket") or "") for row in rows),
    }


def planned_record(validation: dict[str, Any], parent_validation: dict[str, Any], priority: int, priority_note: str) -> dict[str, Any]:
    notes = {
        "source": SOURCE,
        "review_summary_path": str(REVIEW_SUMMARY_PATH),
        "review_jsonl_path": str(REVIEW_JSONL_PATH),
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
        "allowed_routes": {
            "route_holy_site_effect_requirement": EXPECTED_EFFECT_REQUIREMENT_COUNT,
            "route_holy_site_name_runtime": EXPECTED_NAME_RUNTIME_COUNT,
        },
        "hold_routes": {
            key: count
            for key, count in dict(validation["route_counts"]).items()
            if key.startswith("hold_")
        },
        "allowed_segment_ids": validation["allowed_segment_ids"],
        "route_counts": dict(validation["route_counts"]),
        "subfamily_counts": dict(validation["subfamily_counts"]),
        "risk_bucket_counts": dict(validation["risk_bucket_counts"]),
        "policy_recommendation": (
            "Use as read-only splitter for holy-site effect/name runtime surfaces. "
            "Do not generate candidates from this policy; dense and structural leftovers remain hold/parser-later."
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
        "scope_description": "Read-only splitter for religion_faith_doctrine holy-site effect/name runtime surfaces.",
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
    base = reports_dir() / f"{stamp()}_religion_faith_doctrine_holy_site_effect_requirement_splitter_policy_registry_{mode}"
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
        "allowed_route_count": EXPECTED_ALLOWED_ROUTE_COUNT,
        "route_counts": dict(validation["route_counts"]),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "next_recommendation": "run read-only dry-run routing for the 11 allowed holy-site rows; no candidate generation",
        "output_files": {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary_json": str(summary_path)},
    }
    with jsonl_path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "religion_faith_doctrine holy_site effect/name splitter policy registry",
        "",
        f"mode: {mode}",
        f"agent_key: {AGENT_KEY}",
        f"allowed_route_count: {EXPECTED_ALLOWED_ROUTE_COUNT}",
        f"registry_created_count: {created}",
        f"registry_updated_count: {updated}",
        "route_counts:",
        *[f"- {count} | {key}" for key, count in validation["route_counts"].most_common()],
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

    summary = read_json(REVIEW_SUMMARY_PATH)
    rows = read_jsonl(REVIEW_JSONL_PATH)
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
