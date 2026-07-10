from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "medium_dynamic_light_article_preposition_policy_registry_v1"
AGENT_KEY = "medium_dynamic_light_article_preposition_policy"
AGENT_TYPE = "subcoordinator"
OPERATIONAL_STATE = "shadow"
DECISION_ROLE = "route_and_split"
PARENT_AGENT_KEY = "medium_dynamic_light_residual_parser_policy"
SCOPE_GROUP = "domain_policy_vote_candidate"
DASHBOARD_GROUP = "Issue Network"
SUMMARY_PATH = Path("reports/20260629_205445_530214_domain_policy_vote_candidate_medium_dynamic_light_residual_article_preposition_review_summary.json")
JSONL_PATH = Path("reports/20260629_205445_530214_domain_policy_vote_candidate_medium_dynamic_light_residual_article_preposition_review.jsonl")
EXPECTED_COUNTS = {
    "article_before_faith_or_culture": 7,
    "preposition_before_faith_or_culture": 7,
    "preposition_before_title_or_realm": 4,
    "contraction_required_de_da_do_dos_das": 1,
    "no_article_proper_name": 1,
    "uncertain_needs_context": 1,
}
ROUTE_SUBTYPES = {
    "article_before_faith_or_culture": "route_article_before_faith_or_culture",
    "preposition_before_faith_or_culture": "route_preposition_before_faith_or_culture",
    "preposition_before_title_or_realm": "route_preposition_before_title_or_realm",
    "contraction_required_de_da_do_dos_das": "route_contraction_required_de_da_do_dos_das",
    "no_article_proper_name": "route_no_article_proper_name",
}
HOLD_SUBTYPES = ["uncertain_needs_context"]


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
    if summary.get("mode") != "read_only_route_review":
        raise SystemExit("summary mode guard failed")
    if summary.get("target_route") != "route_residual_article_preposition_title_faith_culture":
        raise SystemExit("target_route guard failed")
    if int(summary.get("review_count") or 0) != 21 or len(rows) != 21:
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
    summary_counts = Counter({str(item["key"]): int(item["count"]) for item in summary.get("subtype_counts") or []})
    if dict(summary_counts) != EXPECTED_COUNTS:
        raise SystemExit(f"summary subtype counts guard failed: {dict(summary_counts)}")

    duplicate_ids = [segment_id for segment_id, count in Counter(int(row["segment_id"]) for row in rows).items() if count > 1]
    if duplicate_ids:
        raise SystemExit(f"duplicate segment ids guard failed: {duplicate_ids[:10]}")

    route_count = sum(subtype_counts[subtype] for subtype in ROUTE_SUBTYPES)
    hold_count = sum(subtype_counts[subtype] for subtype in HOLD_SUBTYPES)
    if route_count + hold_count != len(rows):
        raise SystemExit("route+hold coverage guard failed")
    return {
        "review_count": len(rows),
        "subtype_counts": subtype_counts,
        "route_count": route_count,
        "hold_count": hold_count,
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
        "review_count": int(validation["review_count"]),
        "subtype_counts": dict(validation["subtype_counts"]),
        "route_subtypes": ROUTE_SUBTYPES,
        "hold_subtypes": HOLD_SUBTYPES,
        "route_count": int(validation["route_count"]),
        "hold_count": int(validation["hold_count"]),
        "policy_recommendation": (
            "Shadow read-only article/preposition splitter only. Classify contexts around title, faith, and culture "
            "tokens; candidate generation remains disabled until token article/gender/contractability guards exist."
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
            "Read-only shadow article/preposition splitter for residual medium_dynamic_light parser policy."
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
        "review_count": int(notes.get("review_count") or 0),
        "subtype_counts": notes.get("subtype_counts"),
        "route_subtypes": notes.get("route_subtypes"),
        "hold_subtypes": notes.get("hold_subtypes"),
    }


def write_reports(
    *,
    apply: bool,
    validation: dict[str, Any],
    parent_validation: dict[str, Any],
    record: dict[str, Any],
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
        "subtype_counts": dict(validation["subtype_counts"]),
        "route_subtypes": ROUTE_SUBTYPES,
        "hold_subtypes": HOLD_SUBTYPES,
        "route_count": int(validation["route_count"]),
        "hold_count": int(validation["hold_count"]),
        "parent_validation": parent_validation,
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
    jsonl_path.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary_json": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "medium_dynamic_light article/preposition policy registry",
        "",
        f"mode: {mode}",
        f"agent_key: {AGENT_KEY}",
        f"parent_agent_key: {PARENT_AGENT_KEY}",
        f"review_count: {validation['review_count']}",
        "",
        "subtype_counts:",
    ]
    lines.extend(f"- {count} | {key}" for key, count in validation["subtype_counts"].most_common())
    lines.extend(
        [
            "",
            "guards:",
            "- candidate_generation_allowed: false",
            "- auto_apply_allowed: false",
            "- lifecycle_allowed: false",
            "- production_release_allowed: false",
            "- segment_state: not_run",
            "- reindex: not_run",
            "- full_production: not_run",
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
        created = 0
        updated = 0

    txt_path, jsonl_path, summary_path = write_reports(
        apply=args.apply,
        validation=validation,
        parent_validation=parent_validation,
        record=record,
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
    print(f"created={created}")
    print(f"updated={updated}")
    print("candidate_generation_allowed=false")
    print("auto_apply_allowed=false")
    print("lifecycle_allowed=false")
    print("production_release_allowed=false")
    print("ran_apply=false")
    print("ran_lifecycle=false")
    print("ran_segment_state=false")
    print("ran_reindex=false")
    print("ran_production_full=false")


if __name__ == "__main__":
    main()
