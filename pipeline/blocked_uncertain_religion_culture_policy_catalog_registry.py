from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "blocked_uncertain_religion_culture_policy_catalog_registry_v1"
AGENT_KEY = "blocked_uncertain_religion_culture_policy"
PARENT_AGENT_KEY = "blocked_uncertain"
EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76
EXPECTED_REGISTERED_AGENTS_BEFORE = 236
EXPECTED_REVIEW_TOTAL = 60
EXPECTED_CHILD_COUNT = 2
EXPECTED_TERMINALIZED_CHILD_COVERAGE = 32
EXPECTED_LEFTOVERS = 28
EXPECTED_SOURCE_DECISION = "needs_blocked_religion_culture_policy"
EXPECTED_PRIMARY_ROUTE = "blocked_uncertain"
CHILD_KEYS = [
    "blocked_uncertain_religion_culture_faith_doctrine_doctrine_tenet_doctrine_group_policy",
    "blocked_uncertain_religion_culture_faith_doctrine_faith_name_policy",
]
PRESERVED_SUBLANES = {
    "needs_blocked_religion_culture_tenet_policy": 19,
    "needs_doctrine_group_name_policy": 3,
    "needs_doctrine_tenet_short_label_policy": 2,
    "needs_faith_doctrine_gender_perspective_policy": 1,
    "needs_faith_doctrine_short_label_policy": 1,
    "blocked_religion_culture_terminal_guard": 1,
    "blocked_religion_culture_reuse_domain_context_religion_holy_site_policy": 1,
}
SCOPE_DESCRIPTION = (
    "Read-only splitter for blocked/uncertain religion-culture routes; delegates to "
    "faith/doctrine terminal guards while preserving tenet, short-label, gender and "
    "domain-reuse leftovers for later routing."
)


def output_paths(apply: bool) -> tuple[Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    suffix = "apply" if apply else "dry_run"
    base = reports_dir / f"{stamp}_blocked_uncertain_religion_culture_policy_catalog_registry_{suffix}"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl")


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"Invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def summary_row(rows: list[dict[str, Any]], label: str) -> dict[str, Any]:
    summaries = [row for row in rows if row.get("record_type") == "summary"]
    if len(summaries) != 1:
        raise SystemExit(f"expected exactly one summary in {label}, got {len(summaries)}")
    return summaries[0]


def sample_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    samples = [row for row in rows if row.get("record_type") == "sample_review"]
    ids = [int(row["segment_id"]) for row in samples]
    if len(ids) != len(set(ids)):
        raise SystemExit("duplicate sample segment_id in religion/culture review")
    return samples


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def registry_preflight(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT agent_key, operational_state, decision_role, priority FROM ml_agent_registry"
    ).fetchall()
    used_priorities = {int(row["priority"]) for row in rows if row["priority"] is not None}
    priority = 62
    while priority in used_priorities:
        priority += 1
    child_rows = conn.execute(
        f"""
        SELECT *
        FROM ml_agent_registry
        WHERE agent_key IN ({','.join('?' for _ in CHILD_KEYS)})
        """,
        CHILD_KEYS,
    ).fetchall()
    child_by_key = {str(row["agent_key"]): dict(row) for row in child_rows}
    return {
        "registered_agents": len(rows),
        "preexisting_agent": any(row["agent_key"] == AGENT_KEY for row in rows),
        "parent_registered": any(row["agent_key"] == PARENT_AGENT_KEY for row in rows),
        "priority": priority,
        "priority_conflict_resolution": "priority_62_available" if priority == 62 else f"priority_62_conflict_used_{priority}",
        "children": child_by_key,
    }


def validate_preflight(preflight: dict[str, Any]) -> None:
    expected_registered = EXPECTED_REGISTERED_AGENTS_BEFORE + (1 if preflight["preexisting_agent"] else 0)
    if int(preflight["registered_agents"]) != expected_registered:
        raise SystemExit(
            f"registry count guard failed: {preflight['registered_agents']} expected {expected_registered}"
        )


def validate_child_registry(preflight: dict[str, Any]) -> None:
    child_by_key = preflight["children"]
    for child_key in CHILD_KEYS:
        row = child_by_key.get(child_key)
        if not row:
            raise SystemExit(f"registered child missing: {child_key}")
        expected = {
            "status": "active",
            "operational_state": "dry_run",
            "agent_type": "symbolic_subpolicy",
            "decision_role": "terminal_guard",
            "scope_group": "blocked_uncertain_router",
            "dashboard_group": "Issue Network",
        }
        for key, value in expected.items():
            if str(row.get(key) or "") != value:
                raise SystemExit(f"child registry guard failed for {child_key}: {key}={row.get(key)} expected {value}")


def validate_inputs(
    *,
    religion_spec: dict[str, Any],
    religion_summary: dict[str, Any],
    religion_samples: list[dict[str, Any]],
    doctrine_apply_summary: dict[str, Any],
    faith_apply_summary: dict[str, Any],
    preflight: dict[str, Any],
) -> dict[str, Any]:
    if religion_spec.get("policy_id") != AGENT_KEY:
        raise SystemExit(f"spec policy_id guard failed: {religion_spec.get('policy_id')}")
    if religion_spec.get("parent_policy") != PARENT_AGENT_KEY:
        raise SystemExit(f"spec parent_policy guard failed: {religion_spec.get('parent_policy')}")
    if int(religion_spec.get("segment_state_run_id") or 0) != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("spec segment_state_run_id guard failed")
    if int(religion_spec.get("ledger_run_id") or 0) != EXPECTED_LEDGER_RUN_ID:
        raise SystemExit("spec ledger_run_id guard failed")
    if len(religion_samples) != EXPECTED_REVIEW_TOTAL:
        raise SystemExit(f"review_total guard failed: {len(religion_samples)} expected {EXPECTED_REVIEW_TOTAL}")
    if int(religion_summary.get("total_reviewed") or 0) != EXPECTED_REVIEW_TOTAL:
        raise SystemExit("summary total_reviewed guard failed")
    if religion_summary.get("parent_policy") != PARENT_AGENT_KEY:
        raise SystemExit("summary parent_policy guard failed")
    if religion_summary.get("policy_id") != AGENT_KEY:
        raise SystemExit("summary policy_id guard failed")
    if religion_summary.get("requires_apply_later") is not False:
        raise SystemExit("summary requires_apply_later guard failed")
    if religion_summary.get("requires_lifecycle_later") is not False:
        raise SystemExit("summary requires_lifecycle_later guard failed")
    if int(religion_summary.get("true_blocked_count") or 0) != 0:
        raise SystemExit("summary true_blocked_count guard failed")
    if int(religion_summary.get("ready_lifecycle_future") or 0) != 0:
        raise SystemExit("ready_lifecycle_future guard failed")
    if int(religion_summary.get("apply_candidates_future") or 0) != 0:
        raise SystemExit("apply_candidates_future guard failed")

    bad_source = [row["segment_id"] for row in religion_samples if row.get("source_decision") != EXPECTED_SOURCE_DECISION]
    if bad_source:
        raise SystemExit(f"source_decision guard failed: {bad_source[:5]}")
    bad_route = [row["segment_id"] for row in religion_samples if row.get("primary_route") != EXPECTED_PRIMARY_ROUTE]
    if bad_route:
        raise SystemExit(f"primary_route guard failed: {bad_route[:5]}")
    if sum(1 for row in religion_samples if row.get("requires_apply_later") is True):
        raise SystemExit("sample requires_apply_later guard failed")
    if sum(1 for row in religion_samples if row.get("requires_lifecycle_later") is True):
        raise SystemExit("sample requires_lifecycle_later guard failed")
    if sum(1 for row in religion_samples if row.get("is_true_blocked") is True):
        raise SystemExit("sample true_blocked guard failed")

    decision_counts = Counter(str(row.get("religion_culture_decision") or "") for row in religion_samples)
    expected_initial = {
        "needs_blocked_religion_culture_faith_doctrine_policy": 39,
        "needs_blocked_religion_culture_tenet_policy": 19,
        "blocked_religion_culture_terminal_guard": 1,
        "blocked_religion_culture_reuse_domain_context_religion_holy_site_policy": 1,
    }
    for decision, count in expected_initial.items():
        if int(decision_counts.get(decision, 0)) != count:
            raise SystemExit(f"review decision guard failed: {decision}={decision_counts.get(decision, 0)} expected {count}")

    if str(doctrine_apply_summary.get("mode") or "") != "apply" or int(doctrine_apply_summary.get("terminal_guard_count") or 0) != 15:
        raise SystemExit("doctrine child apply summary guard failed")
    if str(faith_apply_summary.get("mode") or "") != "apply" or int(faith_apply_summary.get("terminal_guard_count") or 0) != 17:
        raise SystemExit("faith child apply summary guard failed")

    validate_child_registry(preflight)
    terminalized_child_coverage = int(doctrine_apply_summary["terminal_guard_count"]) + int(faith_apply_summary["terminal_guard_count"])
    if terminalized_child_coverage != EXPECTED_TERMINALIZED_CHILD_COVERAGE:
        raise SystemExit(f"terminalized child coverage guard failed: {terminalized_child_coverage}")
    if sum(PRESERVED_SUBLANES.values()) != EXPECTED_LEFTOVERS:
        raise SystemExit("preserved sublane sum guard failed")

    return {
        "review_total": len(religion_samples),
        "registered_child_found": len(CHILD_KEYS),
        "terminalized_child_coverage": terminalized_child_coverage,
        "blocked_religion_culture_leftovers_projected": EXPECTED_LEFTOVERS,
        "true_blocked_count": 0,
        "requires_apply_later": 0,
        "requires_lifecycle_later": 0,
        "decision_counts": decision_counts,
    }


def planned_record(validation: dict[str, Any], preflight: dict[str, Any]) -> dict[str, Any]:
    notes = {
        "source": SOURCE,
        "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
        "ledger_run_id": EXPECTED_LEDGER_RUN_ID,
        "read_only_component": True,
        "terminal_policy": False,
        "policy_shape": "route_and_split_parent",
        "primary_route": EXPECTED_PRIMARY_ROUTE,
        "source_decision": EXPECTED_SOURCE_DECISION,
        "review_total": int(validation["review_total"]),
        "terminalized_child_coverage": int(validation["terminalized_child_coverage"]),
        "blocked_religion_culture_leftovers_projected": int(validation["blocked_religion_culture_leftovers_projected"]),
        "registered_child_policies": CHILD_KEYS,
        "registered_child_found": int(validation["registered_child_found"]),
        "parent_policy": PARENT_AGENT_KEY,
        "parent_registered": bool(preflight["parent_registered"]),
        "true_blocked_count": 0,
        "requires_apply_later": 0,
        "requires_lifecycle_later": 0,
        "auto_apply_allowed": 0,
        "production_release_allowed": 0,
        "lifecycle_allowed": 0,
        "preserved_sublanes": PRESERVED_SUBLANES,
        "decision_counts": dict(validation["decision_counts"]),
        "priority_conflict_resolution": preflight["priority_conflict_resolution"],
        "policy_chain": [
            "blocked_uncertain",
            "blocked_uncertain_religion_culture_policy",
        ],
    }
    return {
        "agent_key": AGENT_KEY,
        "agent_type": "subcoordinator",
        "parent_agent_key": PARENT_AGENT_KEY,
        "status": "active",
        "operational_state": "shadow",
        "decision_role": "route_and_split",
        "scope_group": "blocked_uncertain_router",
        "scope_description": SCOPE_DESCRIPTION,
        "dashboard_group": "Issue Network",
        "priority": int(preflight["priority"]),
        "notes_json": notes,
    }


def dry_run_counts(validation: dict[str, Any]) -> dict[str, int]:
    return {
        "policies_to_register": 1,
        "review_total": int(validation["review_total"]),
        "registered_child_found": int(validation["registered_child_found"]),
        "terminalized_child_coverage": int(validation["terminalized_child_coverage"]),
        "blocked_religion_culture_leftovers_projected": int(validation["blocked_religion_culture_leftovers_projected"]),
        "true_blocked_count": 0,
        "requires_apply_later": 0,
        "requires_lifecycle_later": 0,
        "auto_apply_allowed": 0,
        "production_release_allowed": 0,
        "lifecycle_allowed": 0,
    }


def assert_apply_guards(counts: dict[str, int]) -> None:
    expected = {
        "policies_to_register": 1,
        "review_total": EXPECTED_REVIEW_TOTAL,
        "registered_child_found": EXPECTED_CHILD_COUNT,
        "terminalized_child_coverage": EXPECTED_TERMINALIZED_CHILD_COVERAGE,
        "blocked_religion_culture_leftovers_projected": EXPECTED_LEFTOVERS,
        "true_blocked_count": 0,
        "requires_apply_later": 0,
        "requires_lifecycle_later": 0,
        "auto_apply_allowed": 0,
        "production_release_allowed": 0,
        "lifecycle_allowed": 0,
    }
    for key, value in expected.items():
        if counts.get(key) != value:
            raise SystemExit(f"apply guard failed: {key}={counts.get(key)} expected {value}")


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
    validation = {
        "exists": True,
        "status": payload.get("status"),
        "operational_state": payload.get("operational_state"),
        "agent_type": payload.get("agent_type"),
        "decision_role": payload.get("decision_role"),
        "parent_agent_key": payload.get("parent_agent_key"),
        "scope_group": payload.get("scope_group"),
        "dashboard_group": payload.get("dashboard_group"),
        "auto_apply_allowed": int(notes.get("auto_apply_allowed") or 0),
        "production_release_allowed": int(notes.get("production_release_allowed") or 0),
        "lifecycle_allowed": int(notes.get("lifecycle_allowed") or 0),
        "review_total": int(notes.get("review_total") or 0),
        "terminalized_child_coverage": int(notes.get("terminalized_child_coverage") or 0),
        "blocked_religion_culture_leftovers_projected": int(notes.get("blocked_religion_culture_leftovers_projected") or 0),
        "source": notes.get("source"),
    }
    expected = {
        "exists": True,
        "status": "active",
        "operational_state": "shadow",
        "agent_type": "subcoordinator",
        "decision_role": "route_and_split",
        "parent_agent_key": PARENT_AGENT_KEY,
        "scope_group": "blocked_uncertain_router",
        "dashboard_group": "Issue Network",
        "auto_apply_allowed": 0,
        "production_release_allowed": 0,
        "lifecycle_allowed": 0,
        "review_total": EXPECTED_REVIEW_TOTAL,
        "terminalized_child_coverage": EXPECTED_TERMINALIZED_CHILD_COVERAGE,
        "blocked_religion_culture_leftovers_projected": EXPECTED_LEFTOVERS,
        "source": SOURCE,
    }
    for key, value in expected.items():
        if validation.get(key) != value:
            raise SystemExit(f"registry validation failed: {key}={validation.get(key)} expected {value}")
    return validation


def write_reports(
    *,
    apply: bool,
    record: dict[str, Any],
    counts: dict[str, int],
    registry_validation: dict[str, Any] | None,
    created: int,
    updated: int,
    preflight: dict[str, Any],
) -> tuple[Path, Path]:
    txt_path, jsonl_path = output_paths(apply)
    mode = "apply" if apply else "dry_run"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps({
            "record_type": "summary",
            "mode": mode,
            **counts,
            "created": created,
            "updated": updated,
            "preexisting_agent": bool(preflight["preexisting_agent"]),
            "parent_registered": bool(preflight["parent_registered"]),
            "priority": record["priority"],
            "priority_conflict_resolution": preflight["priority_conflict_resolution"],
            "registry_validation": registry_validation or {},
            "requires_lifecycle_later": False,
            "requires_apply_later": False,
        }, ensure_ascii=False, sort_keys=True) + "\n")
        handle.write(json.dumps({
            "record_type": "registry_plan",
            "mode": mode,
            "agent_key": record["agent_key"],
            "agent_type": record["agent_type"],
            "parent_agent_key": record["parent_agent_key"],
            "status": record["status"],
            "operational_state": record["operational_state"],
            "decision_role": record["decision_role"],
            "scope_group": record["scope_group"],
            "dashboard_group": record["dashboard_group"],
            "priority": record["priority"],
            "terminal_policy": False,
            "read_only_component": True,
            "preserved_sublanes": PRESERVED_SUBLANES,
            "requires_lifecycle_later": False,
            "requires_apply_later": False,
        }, ensure_ascii=False, sort_keys=True) + "\n")
    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(f"Blocked uncertain religion/culture policy catalog registry {mode}\n\n")
        for key in [
            "policies_to_register",
            "review_total",
            "registered_child_found",
            "terminalized_child_coverage",
            "blocked_religion_culture_leftovers_projected",
            "true_blocked_count",
            "requires_apply_later",
            "requires_lifecycle_later",
            "auto_apply_allowed",
            "production_release_allowed",
            "lifecycle_allowed",
        ]:
            handle.write(f"- {key}: {counts[key]}\n")
        handle.write(f"- parent_registered: {bool(preflight['parent_registered'])}\n")
        handle.write(f"- priority: {record['priority']}\n")
        handle.write(f"- priority_conflict_resolution: {preflight['priority_conflict_resolution']}\n")
        handle.write(f"- created: {created}\n")
        handle.write(f"- updated: {updated}\n")
        handle.write("\nPreserved sublanes\n")
        for key, value in PRESERVED_SUBLANES.items():
            handle.write(f"- {key}: {value}\n")
        handle.write("\nNetwork effect if created\n")
        handle.write("- registered_agents +1, shadow_agents +1, splitter_agents +1, Issue Network +1\n")
        handle.write("- operational_agents unchanged, dry_run_agents unchanged\n")
        if registry_validation:
            handle.write(f"\nregistry_validation: {json.dumps(registry_validation, ensure_ascii=False, sort_keys=True)}\n")
        handle.write("\nAgent\n")
        handle.write(f"- {record['agent_key']} [{record['agent_type']}/{record['operational_state']}/{record['decision_role']}]\n")
    return txt_path, jsonl_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Register blocked_uncertain religion/culture read-only splitter.")
    parser.add_argument("--religion-culture-jsonl", required=True, type=Path)
    parser.add_argument("--religion-culture-spec-json", required=True, type=Path)
    parser.add_argument("--doctrine-group-registry-jsonl", required=True, type=Path)
    parser.add_argument("--faith-name-registry-jsonl", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    religion_rows = read_jsonl(args.religion_culture_jsonl)
    religion_spec = read_json(args.religion_culture_spec_json)
    doctrine_summary = summary_row(read_jsonl(args.doctrine_group_registry_jsonl), "doctrine registry")
    faith_summary = summary_row(read_jsonl(args.faith_name_registry_jsonl), "faith registry")
    religion_summary = summary_row(religion_rows, "religion/culture review")
    religion_samples = sample_rows(religion_rows)
    with connect_readonly() as ro_conn:
        preflight = registry_preflight(ro_conn)
    validate_preflight(preflight)
    validation = validate_inputs(
        religion_spec=religion_spec,
        religion_summary=religion_summary,
        religion_samples=religion_samples,
        doctrine_apply_summary=doctrine_summary,
        faith_apply_summary=faith_summary,
        preflight=preflight,
    )
    record = planned_record(validation, preflight)
    counts = dry_run_counts(validation)
    assert_apply_guards(counts)

    created = 0
    updated = 0
    registry_validation = None
    if args.apply:
        settings = db.load_settings()
        with db.connect(settings) as conn:
            created, updated = upsert_record(conn, record)
            conn.commit()
            registry_validation = validate_registry(conn)
    txt_path, jsonl_path = write_reports(
        apply=args.apply,
        record=record,
        counts=counts,
        registry_validation=registry_validation,
        created=created,
        updated=updated,
        preflight=preflight,
    )
    print(f"txt: {txt_path}")
    print(f"jsonl: {jsonl_path}")
    print(f"mode: {'apply' if args.apply else 'dry_run'}")
    for key, value in counts.items():
        print(f"{key}: {value}")
    print(f"parent_registered: {bool(preflight['parent_registered'])}")
    print(f"priority: {record['priority']}")
    print(f"priority_conflict_resolution: {preflight['priority_conflict_resolution']}")
    print(f"created: {created}")
    print(f"updated: {updated}")
    print(f"preexisting_agent: {preflight['preexisting_agent']}")
    if registry_validation:
        print(f"registry_validation: {json.dumps(registry_validation, ensure_ascii=False, sort_keys=True)}")


if __name__ == "__main__":
    main()
