from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_religion_name_route_subpolicy_registry_v1"
AGENT_KEY = "domain_policy_vote_candidate_religion_name_route_subpolicy"
AGENT_TYPE = "subcoordinator"
OPERATIONAL_STATE = "shadow"
DECISION_ROLE = "route_and_split"
PARENT_AGENT_KEY = "domain_policy_vote_candidate_religion_faith_getter_role_policy"
SCOPE_GROUP = "domain_policy_vote_candidate"
DASHBOARD_GROUP = "Issue Network"
SUMMARY_PATH = Path("reports/20260630_083757_445077_domain_policy_vote_candidate_religion_faith_getter_religion_name_route_review_summary.json")
JSONL_PATH = Path("reports/20260630_083757_445077_domain_policy_vote_candidate_religion_faith_getter_religion_name_route_review.jsonl")
EXPECTED_COUNTS = {
    "placeholder_debug_hold": 44,
    "holy_site_effect_or_requirement": 6,
    "religion_family_reference": 4,
    "generic_religion_name_reference": 3,
    "holy_war_fervor_context": 2,
    "religion_family_requirement": 2,
    "faith_conversion_text": 1,
    "ui_tooltip_religion_reference": 1,
}
ROUTE_SUBTYPES = {
    "faith_conversion_text": "route_religion_name_faith_conversion_text",
    "religion_family_reference": "route_religion_name_family_reference",
    "generic_religion_name_reference": "route_religion_name_generic_reference",
    "ui_tooltip_religion_reference": "route_religion_name_ui_tooltip_reference",
}
HOLD_SUBTYPES = {
    "placeholder_debug_hold": "hold_religion_name_placeholder_debug",
    "holy_site_effect_or_requirement": "hold_religion_name_holy_site_effect",
    "holy_war_fervor_context": "hold_religion_name_holy_war_fervor",
    "religion_family_requirement": "hold_religion_name_family_requirement",
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


def validate_parent(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM ml_agent_registry WHERE agent_key = ?", (PARENT_AGENT_KEY,)).fetchone()
    if row is None:
        raise SystemExit(f"missing parent agent registry record: {PARENT_AGENT_KEY}")
    payload = dict(row)
    notes = json.loads(payload.get("notes_json") or "{}")
    return {
        "exists": True,
        "agent_key": payload.get("agent_key"),
        "operational_state": payload.get("operational_state"),
        "decision_role": payload.get("decision_role"),
        "candidate_generation_allowed": bool(notes.get("candidate_generation_allowed")),
        "auto_apply_allowed": bool(notes.get("auto_apply_allowed")),
        "lifecycle_allowed": bool(notes.get("lifecycle_allowed")),
        "production_release_allowed": bool(notes.get("production_release_allowed")),
    }


def validate_inputs(summary: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    if summary.get("mode") != "read_only_route_review":
        raise SystemExit("summary mode guard failed")
    if summary.get("target_route") != "route_faith_getter_religion_name":
        raise SystemExit("target_route guard failed")
    if int(summary.get("review_count") or 0) != 63 or len(rows) != 63:
        raise SystemExit("review_count guard failed")
    if summary.get("count_matches_expected") is not True:
        raise SystemExit("count_matches_expected guard failed")
    if summary.get("production_full_recommended_now") is not False:
        raise SystemExit("production_full_recommended_now guard failed")
    if summary.get("source_changed") is not False or summary.get("output_changed") is not False:
        raise SystemExit("source/output changed guard failed")

    subtype_counts = Counter(str(row.get("operational_subtype") or "") for row in rows)
    if dict(subtype_counts) != EXPECTED_COUNTS:
        raise SystemExit(f"subtype counts guard failed: {dict(subtype_counts)} expected {EXPECTED_COUNTS}")
    duplicate_ids = [segment_id for segment_id, count in Counter(int(row["segment_id"]) for row in rows).items() if count > 1]
    if duplicate_ids:
        raise SystemExit(f"duplicate segment ids guard failed: {duplicate_ids[:10]}")
    route_count = sum(subtype_counts[subtype] for subtype in ROUTE_SUBTYPES)
    hold_count = sum(subtype_counts[subtype] for subtype in HOLD_SUBTYPES)
    if route_count != 9 or hold_count != 54 or route_count + hold_count != len(rows):
        raise SystemExit("route/hold count guard failed")
    return {"review_count": len(rows), "subtype_counts": subtype_counts, "route_count": route_count, "hold_count": hold_count}


def planned_record(validation: dict[str, Any], parent_validation: dict[str, Any]) -> dict[str, Any]:
    notes = {
        "source": SOURCE,
        "summary_path": str(SUMMARY_PATH),
        "jsonl_path": str(JSONL_PATH),
        "agent_key": AGENT_KEY,
        "agent_type": AGENT_TYPE,
        "operational_state": OPERATIONAL_STATE,
        "decision_role": DECISION_ROLE,
        "parent_agent_key": PARENT_AGENT_KEY,
        "parent_validation": parent_validation,
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
        "subtype_counts": dict(validation["subtype_counts"]),
        "route_subtypes": ROUTE_SUBTYPES,
        "hold_subtypes": HOLD_SUBTYPES,
        "route_count": int(validation["route_count"]),
        "hold_count": int(validation["hold_count"]),
        "policy_recommendation": (
            "Shadow read-only religion_name route splitter. Hold debug/placeholder, holy-site, holy-war, "
            "and religion-family requirement surfaces; route only the 9 non-held rows for later human/policy review."
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
        "scope_description": "Read-only shadow splitter for route_faith_getter_religion_name residual subtypes.",
        "dashboard_group": DASHBOARD_GROUP,
        "priority": 41,
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
        "route_subtypes": notes.get("route_subtypes"),
        "hold_subtypes": notes.get("hold_subtypes"),
        "route_count": notes.get("route_count"),
        "hold_count": notes.get("hold_count"),
    }


def write_reports(*, apply: bool, record: dict[str, Any], validation: dict[str, Any], created: int, updated: int, registry_validation: dict[str, Any]) -> tuple[Path, Path, Path]:
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
        "subtype_counts": dict(validation["subtype_counts"]),
        "route_subtypes": ROUTE_SUBTYPES,
        "hold_subtypes": HOLD_SUBTYPES,
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
        "domain_policy_vote_candidate religion_name route subpolicy registry",
        "",
        f"mode: {mode}",
        f"agent_key: {AGENT_KEY}",
        f"review_count: {validation['review_count']}",
        f"route_count: {validation['route_count']}",
        f"hold_count: {validation['hold_count']}",
        "",
        "route_subtypes:",
    ]
    lines.extend(f"- {subtype}: {route}" for subtype, route in ROUTE_SUBTYPES.items())
    lines.extend(["", "hold_subtypes:"])
    lines.extend(f"- {subtype}: {route}" for subtype, route in HOLD_SUBTYPES.items())
    lines.extend(["", "subtype_counts:"])
    lines.extend(f"- {count} | {subtype}" for subtype, count in validation["subtype_counts"].most_common())
    lines.extend(["", "guards:", "- candidate_generation_allowed: false", "- auto_apply_allowed: false", "- lifecycle_allowed: false", "- production_release_allowed: false", "- apply: not_run", "- lifecycle: not_run", "- segment_state: not_run", "- reindex: not_run", "- full_production: not_run", "", f"created: {created}", f"updated: {updated}"])
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    summary = read_json(SUMMARY_PATH)
    rows = read_jsonl(JSONL_PATH)
    validation = validate_inputs(summary, rows)
    created = 0
    updated = 0
    if args.apply:
        with db.connect(db.load_settings()) as conn:
            parent_validation = validate_parent(conn)
            record = planned_record(validation, parent_validation)
            created, updated = upsert_record(conn, record)
            conn.commit()
            registry_validation = validate_registry(conn)
    else:
        with connect_readonly() as conn:
            parent_validation = validate_parent(conn)
            record = planned_record(validation, parent_validation)
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
    print(f"route_count={validation['route_count']}")
    print(f"hold_count={validation['hold_count']}")
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
