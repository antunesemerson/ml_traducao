from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "blocked_uncertain_doctrine_group_terminal_registry_v1"
AGENT_KEY = "blocked_uncertain_religion_culture_faith_doctrine_doctrine_tenet_doctrine_group_policy"
PARENT_AGENT_KEY = "blocked_uncertain_religion_culture_faith_doctrine_doctrine_tenet_policy"
EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76
EXPECTED_REGISTERED_AGENTS_BEFORE = 232
EXPECTED_REVIEW_TOTAL = 18
EXPECTED_TERMINAL_COUNT = 15
EXPECTED_LEFTOVER_COUNT = 3
EXPECTED_SOURCE_DECISION = "needs_doctrine_tenet_doctrine_group_policy"
EXPECTED_PRIMARY_ROUTE = "blocked_uncertain"
EXPECTED_TERMINAL_DECISION = "doctrine_group_terminal_guard_with_domain_guard"
EXPECTED_LEFTOVER_DECISION = "needs_doctrine_group_name_policy"
SCOPE_DESCRIPTION = (
    "Read-only terminal guard for blocked/uncertain religion-culture faith-doctrine "
    "doctrine-group routes, with domain guard and no apply/lifecycle permission."
)


def output_paths(apply: bool) -> tuple[Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    suffix = "apply" if apply else "dry_run"
    base = reports_dir / f"{stamp}_blocked_uncertain_doctrine_group_terminal_registry_{suffix}"
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


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def review_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    summaries = [row for row in rows if row.get("record_type") == "summary"]
    if len(summaries) != 1:
        raise SystemExit(f"expected exactly one review summary, got {len(summaries)}")
    return summaries[0]


def sample_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    samples = [row for row in rows if row.get("record_type") == "sample_review"]
    ids = [int(row["segment_id"]) for row in samples]
    if len(ids) != len(set(ids)):
        raise SystemExit("duplicate sample segment_id in review")
    return samples


def registry_preflight(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT agent_key, operational_state, decision_role, priority FROM ml_agent_registry"
    ).fetchall()
    own_exists = any(row["agent_key"] == AGENT_KEY for row in rows)
    parent_registered = any(row["agent_key"] == PARENT_AGENT_KEY for row in rows)
    used_priorities = {int(row["priority"]) for row in rows if row["priority"] is not None}
    priority = 45
    while priority in used_priorities:
        priority += 1
    return {
        "registered_agents": len(rows),
        "operational_agents": sum(1 for row in rows if row["operational_state"] == "operational"),
        "dry_run_agents": sum(1 for row in rows if row["operational_state"] == "dry_run"),
        "shadow_agents": sum(1 for row in rows if row["operational_state"] == "shadow"),
        "terminal_guard_agents": sum(1 for row in rows if row["decision_role"] == "terminal_guard"),
        "preexisting_agent": own_exists,
        "parent_registered": parent_registered,
        "priority": priority,
        "priority_conflict_resolution": "priority_45_available" if priority == 45 else f"priority_45_conflict_used_{priority}",
    }


def validate_preflight(preflight: dict[str, Any]) -> None:
    expected_registered = EXPECTED_REGISTERED_AGENTS_BEFORE + (1 if preflight["preexisting_agent"] else 0)
    if int(preflight["registered_agents"]) != expected_registered:
        raise SystemExit(
            f"registry guard failed: registered_agents={preflight['registered_agents']} expected {expected_registered}"
        )
    expected = {
        "operational_agents": 33,
        "dry_run_agents": 24 + (1 if preflight["preexisting_agent"] else 0),
        "shadow_agents": 89,
        "terminal_guard_agents": 18 + (1 if preflight["preexisting_agent"] else 0),
    }
    for key, value in expected.items():
        if int(preflight[key]) != value:
            raise SystemExit(f"registry guard failed: {key}={preflight[key]} expected {value}")


def validate_inputs(*, spec: dict[str, Any], summary: dict[str, Any], samples: list[dict[str, Any]]) -> dict[str, Any]:
    if spec.get("policy_id") != AGENT_KEY:
        raise SystemExit(f"spec policy_id guard failed: {spec.get('policy_id')}")
    if spec.get("parent_policy") != PARENT_AGENT_KEY:
        raise SystemExit(f"spec parent_policy guard failed: {spec.get('parent_policy')}")
    if int(spec.get("segment_state_run_id") or 0) != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("spec segment_state_run_id guard failed")
    if int(spec.get("ledger_run_id") or 0) != EXPECTED_LEDGER_RUN_ID:
        raise SystemExit("spec ledger_run_id guard failed")
    if len(samples) != EXPECTED_REVIEW_TOTAL:
        raise SystemExit(f"review_total guard failed: {len(samples)} expected {EXPECTED_REVIEW_TOTAL}")
    if int(summary.get("total_reviewed") or 0) != EXPECTED_REVIEW_TOTAL:
        raise SystemExit("summary total_reviewed guard failed")
    if int(summary.get("terminal_guard_count") or 0) != EXPECTED_TERMINAL_COUNT:
        raise SystemExit("summary terminal_guard_count guard failed")
    if int(summary.get("new_sublane_candidate_count") or 0) != EXPECTED_LEFTOVER_COUNT:
        raise SystemExit("summary new_sublane_candidate_count guard failed")
    if int(summary.get("true_blocked_count") or 0) != 0:
        raise SystemExit("summary true_blocked_count guard failed")
    if int(summary.get("ready_lifecycle_future") or 0) != 0:
        raise SystemExit("summary ready_lifecycle_future guard failed")
    if int(summary.get("apply_candidates_future") or 0) != 0:
        raise SystemExit("summary apply_candidates_future guard failed")
    if summary.get("requires_apply_later") is not False:
        raise SystemExit("summary requires_apply_later guard failed")
    if summary.get("requires_lifecycle_later") is not False:
        raise SystemExit("summary requires_lifecycle_later guard failed")

    bad_source = [row["segment_id"] for row in samples if row.get("source_decision") != EXPECTED_SOURCE_DECISION]
    if bad_source:
        raise SystemExit(f"source_decision guard failed: {bad_source[:5]}")
    bad_parent = [row["segment_id"] for row in samples if row.get("parent_policy") != PARENT_AGENT_KEY]
    if bad_parent:
        raise SystemExit(f"parent_policy guard failed: {bad_parent[:5]}")
    bad_route = [row["segment_id"] for row in samples if row.get("primary_route") != EXPECTED_PRIMARY_ROUTE]
    if bad_route:
        raise SystemExit(f"primary_route guard failed: {bad_route[:5]}")

    requires_apply = sum(1 for row in samples if row.get("requires_apply_later") is True)
    requires_lifecycle = sum(1 for row in samples if row.get("requires_lifecycle_later") is True)
    true_blocked = sum(1 for row in samples if row.get("is_true_blocked") is True)
    if requires_apply:
        raise SystemExit(f"requires_apply_later guard failed: {requires_apply}")
    if requires_lifecycle:
        raise SystemExit(f"requires_lifecycle_later guard failed: {requires_lifecycle}")
    if true_blocked:
        raise SystemExit(f"true_blocked guard failed: {true_blocked}")

    decision_counts = Counter(str(row.get("doctrine_group_decision") or "") for row in samples)
    terminal_count = int(decision_counts.get(EXPECTED_TERMINAL_DECISION, 0))
    leftover_count = int(decision_counts.get(EXPECTED_LEFTOVER_DECISION, 0))
    if terminal_count != EXPECTED_TERMINAL_COUNT:
        raise SystemExit(f"{EXPECTED_TERMINAL_DECISION} guard failed: {terminal_count}")
    if leftover_count != EXPECTED_LEFTOVER_COUNT:
        raise SystemExit(f"{EXPECTED_LEFTOVER_DECISION} guard failed: {leftover_count}")
    return {
        "review_total": len(samples),
        "terminal_guard_count": terminal_count,
        "doctrine_group_terminal_guard_with_domain_guard": terminal_count,
        "new_sublane_candidate_count": leftover_count,
        "needs_doctrine_group_name_policy": leftover_count,
        "true_blocked_count": true_blocked,
        "requires_apply_later": requires_apply,
        "requires_lifecycle_later": requires_lifecycle,
        "decision_counts": decision_counts,
    }


def planned_record(validation: dict[str, Any], preflight: dict[str, Any]) -> dict[str, Any]:
    notes = {
        "source": SOURCE,
        "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
        "ledger_run_id": EXPECTED_LEDGER_RUN_ID,
        "review_total": int(validation["review_total"]),
        "terminal_guard_count": int(validation["terminal_guard_count"]),
        "doctrine_group_terminal_guard_with_domain_guard": int(validation["doctrine_group_terminal_guard_with_domain_guard"]),
        "new_sublane_candidate_count": int(validation["new_sublane_candidate_count"]),
        "needs_doctrine_group_name_policy": int(validation["needs_doctrine_group_name_policy"]),
        "true_blocked_count": int(validation["true_blocked_count"]),
        "requires_apply_later": 0,
        "requires_lifecycle_later": 0,
        "auto_apply_allowed": 0,
        "production_release_allowed": 0,
        "lifecycle_allowed": 0,
        "read_only_component": True,
        "terminal_policy": True,
        "terminal_guard": True,
        "domain_guard": True,
        "parent_policy": PARENT_AGENT_KEY,
        "parent_registered": bool(preflight["parent_registered"]),
        "registered_parent_candidates": [],
        "primary_route": EXPECTED_PRIMARY_ROUTE,
        "source_decision": EXPECTED_SOURCE_DECISION,
        "policy_shape": "terminal_guard",
        "priority_conflict_resolution": preflight["priority_conflict_resolution"],
        "policy_chain": [
            "blocked_uncertain",
            "blocked_uncertain_religion_culture_policy",
            "blocked_uncertain_religion_culture_faith_doctrine_policy",
            "blocked_uncertain_religion_culture_faith_doctrine_doctrine_tenet_policy",
            "blocked_uncertain_religion_culture_faith_doctrine_doctrine_tenet_doctrine_group_policy",
        ],
        "leftovers": {
            "needs_doctrine_group_name_policy": int(validation["needs_doctrine_group_name_policy"]),
        },
        "decision_counts": dict(validation["decision_counts"]),
    }
    return {
        "agent_key": AGENT_KEY,
        "agent_type": "symbolic_subpolicy",
        "parent_agent_key": PARENT_AGENT_KEY,
        "status": "active",
        "operational_state": "dry_run",
        "decision_role": "terminal_guard",
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
        "terminal_guard_count": int(validation["terminal_guard_count"]),
        "doctrine_group_terminal_guard_with_domain_guard": int(validation["doctrine_group_terminal_guard_with_domain_guard"]),
        "needs_doctrine_group_name_policy": int(validation["needs_doctrine_group_name_policy"]),
        "true_blocked_count": int(validation["true_blocked_count"]),
        "requires_apply_later": int(validation["requires_apply_later"]),
        "requires_lifecycle_later": int(validation["requires_lifecycle_later"]),
        "auto_apply_allowed": 0,
        "production_release_allowed": 0,
        "lifecycle_allowed": 0,
    }


def assert_apply_guards(counts: dict[str, int]) -> None:
    expected = {
        "policies_to_register": 1,
        "review_total": EXPECTED_REVIEW_TOTAL,
        "terminal_guard_count": EXPECTED_TERMINAL_COUNT,
        "doctrine_group_terminal_guard_with_domain_guard": EXPECTED_TERMINAL_COUNT,
        "needs_doctrine_group_name_policy": EXPECTED_LEFTOVER_COUNT,
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
        "domain_guard": bool(notes.get("domain_guard")),
        "true_blocked_count": int(notes.get("true_blocked_count") or 0),
        "review_total": int(notes.get("review_total") or 0),
        "terminal_guard_count": int(notes.get("terminal_guard_count") or 0),
        "source": notes.get("source"),
    }
    expected = {
        "exists": True,
        "status": "active",
        "operational_state": "dry_run",
        "agent_type": "symbolic_subpolicy",
        "decision_role": "terminal_guard",
        "parent_agent_key": PARENT_AGENT_KEY,
        "scope_group": "blocked_uncertain_router",
        "dashboard_group": "Issue Network",
        "auto_apply_allowed": 0,
        "production_release_allowed": 0,
        "lifecycle_allowed": 0,
        "domain_guard": True,
        "true_blocked_count": 0,
        "review_total": EXPECTED_REVIEW_TOTAL,
        "terminal_guard_count": EXPECTED_TERMINAL_COUNT,
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
            "terminal_policy": True,
            "terminal_guard": True,
            "domain_guard": True,
            "read_only_component": True,
            "requires_lifecycle_later": False,
            "requires_apply_later": False,
        }, ensure_ascii=False, sort_keys=True) + "\n")
    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(f"Blocked uncertain doctrine-group terminal registry {mode}\n\n")
        for key in [
            "policies_to_register",
            "review_total",
            "terminal_guard_count",
            "doctrine_group_terminal_guard_with_domain_guard",
            "needs_doctrine_group_name_policy",
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
        if registry_validation:
            handle.write(f"- registry_validation: {json.dumps(registry_validation, ensure_ascii=False, sort_keys=True)}\n")
        handle.write("\nAgent\n")
        handle.write(f"- {record['agent_key']} [{record['agent_type']}/{record['operational_state']}/{record['decision_role']}]\n")
    return txt_path, jsonl_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Register blocked uncertain doctrine-group terminal read-only policy.")
    parser.add_argument("--review-jsonl", required=True, type=Path)
    parser.add_argument("--spec-json", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    spec = read_json(args.spec_json)
    rows = read_jsonl(args.review_jsonl)
    summary = review_summary(rows)
    samples = sample_rows(rows)
    with connect_readonly() as ro_conn:
        preflight = registry_preflight(ro_conn)
    validate_preflight(preflight)
    validation = validate_inputs(spec=spec, summary=summary, samples=samples)
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
