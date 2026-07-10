from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "medium_dynamic_light_selectlocalization_affix_policy_registry_v1"
AGENT_KEY = "medium_dynamic_light_selectlocalization_affix_policy"
AGENT_TYPE = "subcoordinator"
OPERATIONAL_STATE = "shadow"
DECISION_ROLE = "route_and_split"
PARENT_AGENT_KEY = "medium_dynamic_light_residual_parser_policy"
SCOPE_GROUP = "domain_policy_vote_candidate"
DASHBOARD_GROUP = "Issue Network"
SUMMARY_PATH = Path("reports/20260630_010318_976484_medium_dynamic_light_selectlocalization_structural_review_summary.json")
JSONL_PATH = Path("reports/20260630_010318_976484_medium_dynamic_light_selectlocalization_structural_review.jsonl")
EXPECTED_COUNT = 2


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


def validate_inputs(summary: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    if summary.get("mode") != "read_only_selectlocalization_structural_review":
        raise SystemExit("summary mode guard failed")
    if int(summary.get("review_count") or 0) != EXPECTED_COUNT or len(rows) != EXPECTED_COUNT:
        raise SystemExit("review_count guard failed")
    if summary.get("recommended_policy_decision") != "subpolicy_read_only":
        raise SystemExit("recommended_policy_decision guard failed")
    if int(summary.get("human_packet_future_count") or 0) != 0:
        raise SystemExit("human_packet_future_count guard failed")
    for key in ("candidate_generation_count", "apply_output_count", "lifecycle_count", "segment_state_count", "reindex_count", "production_full_count"):
        if int(summary.get(key) or 0) != 0:
            raise SystemExit(f"{key} guard failed")
    if summary.get("source_changed") is not False or summary.get("output_changed") is not False:
        raise SystemExit("source/output changed guard failed")

    subtype_counts = Counter(str(row.get("structural_subtype") or "") for row in rows)
    expected_subtypes = {
        "external_prefix_before_selectlocalization_pipe_l": 1,
        "external_suffix_after_selectlocalization": 1,
    }
    if dict(subtype_counts) != expected_subtypes:
        raise SystemExit(f"subtype counts guard failed: {dict(subtype_counts)}")
    if any(row.get("token_internal_change_allowed") is not False for row in rows):
        raise SystemExit("token_internal_change_allowed guard failed")
    return {
        "review_count": len(rows),
        "subtype_counts": subtype_counts,
        "affix_counts": Counter(str(row.get("external_affix_position") or "") for row in rows),
        "risk_counts": Counter(str(row.get("structural_risk") or "") for row in rows),
        "pipe_modifier_present_count": sum(1 for row in rows if row.get("pipe_modifier_present") is True),
        "segment_ids": sorted(int(row["segment_id"]) for row in rows),
    }


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
        "detection_objective": "Route SelectLocalization/Select_CString surfaces with external affixes and pipe modifiers for structural review.",
        "route_subtypes": {
            "external_prefix_before_selectlocalization_pipe_l": "route_selectlocalization_external_prefix_pipe",
            "external_suffix_after_selectlocalization": "route_selectlocalization_external_suffix_affix",
        },
        "review_count": int(validation["review_count"]),
        "subtype_counts": dict(validation["subtype_counts"]),
        "affix_counts": dict(validation["affix_counts"]),
        "risk_counts": dict(validation["risk_counts"]),
        "pipe_modifier_present_count": int(validation["pipe_modifier_present_count"]),
        "segment_ids": validation["segment_ids"],
        "policy_recommendation": (
            "Shadow read-only splitter only. Keep SelectLocalization token internals opaque; route external affix "
            "and pipe modifier cases for parser review before any human packet or apply."
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
        "scope_description": "Read-only shadow SelectLocalization affix/pipe splitter for medium_dynamic_light residual parser policy.",
        "dashboard_group": DASHBOARD_GROUP,
        "priority": 40,
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
        "review_count": int(notes.get("review_count") or 0),
        "route_subtypes": notes.get("route_subtypes"),
    }


def write_reports(mode: str, validation: dict[str, Any], parent_validation: dict[str, Any], record: dict[str, Any], created: int, updated: int, registry_validation: dict[str, Any]) -> None:
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
        "review_count": int(validation["review_count"]),
        "subtype_counts": dict(validation["subtype_counts"]),
        "affix_counts": dict(validation["affix_counts"]),
        "risk_counts": dict(validation["risk_counts"]),
        "pipe_modifier_present_count": int(validation["pipe_modifier_present_count"]),
        "parent_validation": parent_validation,
        "created": created,
        "updated": updated,
        "registry_validation": registry_validation,
        "candidate_generation_count": 0,
        "apply_output_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "output_files": {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary_json": str(summary_path)},
    }
    jsonl_path.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    txt_path.write_text(
        "\n".join(
            [
                "medium_dynamic_light SelectLocalization affix policy registry",
                "",
                f"mode: {mode}",
                f"agent_key: {AGENT_KEY}",
                f"parent_agent_key: {PARENT_AGENT_KEY}",
                f"review_count: {validation['review_count']}",
                "",
                "subtype_counts:",
                *[f"- {count} | {key}" for key, count in validation["subtype_counts"].most_common()],
                "",
                "guards:",
                "- candidate_generation: not_run",
                "- apply: not_run",
                "- lifecycle: not_run",
                "- segment_state: not_run",
                "- reindex: not_run",
                "- full_production: not_run",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


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
        settings = db.load_settings()
        with db.connect(settings) as conn:
            conn.row_factory = sqlite3.Row
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
    write_reports("apply" if args.apply else "dry_run", validation, parent_validation, record, created, updated, registry_validation)


if __name__ == "__main__":
    main()
