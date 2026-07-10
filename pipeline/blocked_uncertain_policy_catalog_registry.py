from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "blocked_uncertain_policy_catalog_registry_v1"
AGENT_KEY = "blocked_uncertain"
PARENT_AGENT_KEY = "macro_lane_router"
EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76
EXPECTED_REGISTERED_AGENTS_BEFORE = 237
EXPECTED_REVIEW_TOTAL = 144
EXPECTED_CHILD_COUNT = 3
EXPECTED_PROJECTED_GAIN = 109
EXPECTED_REMAINING = 35
EXPECTED_COVERAGE_BEFORE = 11581
EXPECTED_COVERAGE_AFTER = 11690
EXPECTED_PENDING_TOTAL = 11725
CHILD_KEYS = [
    "blocked_uncertain_religion_culture_policy",
    "blocked_uncertain_token_integrity_debug_marker_policy",
    "blocked_uncertain_gender_perspective_policy",
]
REMAINING_SUBLANES = {
    "blocked_religion_culture_leftovers_projected": 28,
    "needs_blocked_language_residual_policy": 6,
    "needs_blocked_name_title_culture_policy": 1,
}
RELIGION_CULTURE_LEFTOVERS = {
    "needs_blocked_religion_culture_tenet_policy": 19,
    "needs_doctrine_group_name_policy": 3,
    "needs_doctrine_tenet_short_label_policy": 2,
    "needs_faith_doctrine_gender_perspective_policy": 1,
    "needs_faith_doctrine_short_label_policy": 1,
    "blocked_religion_culture_terminal_guard": 1,
    "blocked_religion_culture_reuse_domain_context_religion_holy_site_policy": 1,
}
SCOPE_DESCRIPTION = (
    "Read-only global route-and-split parent for blocked/uncertain segments; ties "
    "religion-culture, token-integrity debug-marker and gender-perspective components "
    "while preserving remaining language/name-title leftovers."
)


def output_paths(apply: bool) -> tuple[Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    suffix = "apply" if apply else "dry_run"
    base = reports_dir / f"{stamp}_blocked_uncertain_policy_catalog_registry_{suffix}"
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
        raise SystemExit("duplicate sample segment_id in blocked review")
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
    priority = 63
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
    return {
        "registered_agents": len(rows),
        "preexisting_agent": any(row["agent_key"] == AGENT_KEY for row in rows),
        "parent_registered": any(row["agent_key"] == PARENT_AGENT_KEY for row in rows),
        "priority": priority,
        "priority_conflict_resolution": "priority_63_available" if priority == 63 else f"priority_63_conflict_used_{priority}",
        "children": {str(row["agent_key"]): dict(row) for row in child_rows},
    }


def validate_preflight(preflight: dict[str, Any]) -> None:
    expected_registered = EXPECTED_REGISTERED_AGENTS_BEFORE + (1 if preflight["preexisting_agent"] else 0)
    if int(preflight["registered_agents"]) != expected_registered:
        raise SystemExit(
            f"registry count guard failed: {preflight['registered_agents']} expected {expected_registered}"
        )


def validate_child_registry(preflight: dict[str, Any]) -> None:
    expected_by_child = {
        "blocked_uncertain_religion_culture_policy": {
            "status": "active",
            "operational_state": "shadow",
            "agent_type": "subcoordinator",
            "decision_role": "route_and_split",
        },
        "blocked_uncertain_token_integrity_debug_marker_policy": {
            "status": "active",
            "operational_state": "dry_run",
            "agent_type": "symbolic_subpolicy",
            "decision_role": "terminal_guard",
        },
        "blocked_uncertain_gender_perspective_policy": {
            "status": "active",
            "operational_state": "dry_run",
            "agent_type": "symbolic_subpolicy",
            "decision_role": "terminal_guard",
        },
    }
    for child_key, expected in expected_by_child.items():
        row = preflight["children"].get(child_key)
        if not row:
            raise SystemExit(f"registered child missing: {child_key}")
        expected = {
            **expected,
            "scope_group": "blocked_uncertain_router",
            "dashboard_group": "Issue Network",
        }
        for key, value in expected.items():
            if str(row.get(key) or "") != value:
                raise SystemExit(f"child registry guard failed for {child_key}: {key}={row.get(key)} expected {value}")
        notes = json.loads(str(row.get("notes_json") or "{}"))
        for flag in ["auto_apply_allowed", "production_release_allowed", "lifecycle_allowed"]:
            if int(notes.get(flag) or 0) != 0:
                raise SystemExit(f"child flag guard failed for {child_key}: {flag}={notes.get(flag)}")


def validate_inputs(
    *,
    blocked_spec: dict[str, Any],
    blocked_samples: list[dict[str, Any]],
    religion_summary: dict[str, Any],
    debug_summary: dict[str, Any],
    gender_summary: dict[str, Any],
    preflight: dict[str, Any],
) -> dict[str, Any]:
    if blocked_spec.get("policy_id") != AGENT_KEY:
        raise SystemExit(f"spec policy_id guard failed: {blocked_spec.get('policy_id')}")
    if int(blocked_spec.get("segment_state_run_id") or 0) != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("spec segment_state_run_id guard failed")
    if int(blocked_spec.get("ledger_run_id") or 0) != EXPECTED_LEDGER_RUN_ID:
        raise SystemExit("spec ledger_run_id guard failed")
    if len(blocked_samples) != EXPECTED_REVIEW_TOTAL:
        raise SystemExit(f"review_total guard failed: {len(blocked_samples)} expected {EXPECTED_REVIEW_TOTAL}")
    if sum(1 for row in blocked_samples if row.get("requires_apply_later") is True):
        raise SystemExit("blocked sample requires_apply_later guard failed")
    if sum(1 for row in blocked_samples if row.get("requires_lifecycle_later") is True):
        raise SystemExit("blocked sample requires_lifecycle_later guard failed")
    if sum(1 for row in blocked_samples if row.get("is_true_blocked") is True):
        raise SystemExit("blocked sample true_blocked guard failed")

    decision_counts = Counter(str(row.get("blocked_decision") or "") for row in blocked_samples)
    expected_decisions = {
        "needs_blocked_religion_culture_policy": 60,
        "needs_blocked_token_integrity_policy": 54,
        "needs_blocked_gender_perspective_policy": 23,
        "needs_blocked_language_residual_policy": 6,
        "needs_blocked_name_title_culture_policy": 1,
    }
    for decision, count in expected_decisions.items():
        if int(decision_counts.get(decision, 0)) != count:
            raise SystemExit(f"blocked decision guard failed: {decision}={decision_counts.get(decision, 0)} expected {count}")

    validate_child_registry(preflight)
    if str(religion_summary.get("mode") or "") != "apply" or int(religion_summary.get("review_total") or 0) != 60:
        raise SystemExit("religion/culture apply summary guard failed")
    if int(religion_summary.get("terminalized_child_coverage") or 0) != 32:
        raise SystemExit("religion/culture terminalized coverage guard failed")
    if int(religion_summary.get("blocked_religion_culture_leftovers_projected") or 0) != 28:
        raise SystemExit("religion/culture leftovers guard failed")
    if str(debug_summary.get("mode") or "") != "apply" or int(debug_summary.get("terminal_guard_count") or 0) != 54:
        raise SystemExit("debug marker apply summary guard failed")
    if str(gender_summary.get("mode") or "") != "apply" or int(gender_summary.get("review_total") or 0) != 23:
        raise SystemExit("gender perspective apply summary guard failed")

    projected_gain = int(religion_summary["terminalized_child_coverage"]) + int(debug_summary["terminal_guard_count"]) + int(gender_summary["review_total"])
    if projected_gain != EXPECTED_PROJECTED_GAIN:
        raise SystemExit(f"projected gain guard failed: {projected_gain} expected {EXPECTED_PROJECTED_GAIN}")
    if sum(REMAINING_SUBLANES.values()) != EXPECTED_REMAINING:
        raise SystemExit("remaining sublane sum guard failed")
    return {
        "review_total": len(blocked_samples),
        "registered_child_found": len(CHILD_KEYS),
        "registered_descendant_terminal_or_reuse_coverage": projected_gain,
        "blocked_uncertain_remaining_without_useful_spec": EXPECTED_REMAINING,
        "true_blocked_count": 0,
        "requires_apply_later": 0,
        "requires_lifecycle_later": 0,
        "religion_culture_registered_parent_coverage": 60,
        "religion_culture_terminalized_child_coverage": 32,
        "token_integrity_debug_marker_coverage": 54,
        "gender_perspective_registered_coverage": 23,
        "decision_counts": decision_counts,
    }


def planned_record(validation: dict[str, Any], preflight: dict[str, Any]) -> dict[str, Any]:
    notes = {
        "source": SOURCE,
        "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
        "ledger_run_id": EXPECTED_LEDGER_RUN_ID,
        "read_only_component": True,
        "terminal_policy": False,
        "policy_shape": "global_route_and_split_parent",
        "primary_route": AGENT_KEY,
        "review_total": int(validation["review_total"]),
        "true_blocked_count": 0,
        "registered_child_found": int(validation["registered_child_found"]),
        "registered_children": CHILD_KEYS,
        "projected_gain_from_registered_children": int(validation["registered_descendant_terminal_or_reuse_coverage"]),
        "registered_descendant_terminal_or_reuse_coverage": int(validation["registered_descendant_terminal_or_reuse_coverage"]),
        "religion_culture_registered_parent_coverage": int(validation["religion_culture_registered_parent_coverage"]),
        "religion_culture_terminalized_child_coverage": int(validation["religion_culture_terminalized_child_coverage"]),
        "token_integrity_debug_marker_coverage": int(validation["token_integrity_debug_marker_coverage"]),
        "gender_perspective_registered_coverage": int(validation["gender_perspective_registered_coverage"]),
        "coverage_before_blocked_uncertain": EXPECTED_COVERAGE_BEFORE,
        "coverage_projected_after_blocked_uncertain": EXPECTED_COVERAGE_AFTER,
        "pending_total": EXPECTED_PENDING_TOTAL,
        "blocked_uncertain_remaining_without_useful_spec": int(validation["blocked_uncertain_remaining_without_useful_spec"]),
        "remaining_sublanes": REMAINING_SUBLANES,
        "religion_culture_leftovers": RELIGION_CULTURE_LEFTOVERS,
        "parent_policy": PARENT_AGENT_KEY,
        "parent_registered": bool(preflight["parent_registered"]),
        "requires_apply_later": 0,
        "requires_lifecycle_later": 0,
        "auto_apply_allowed": 0,
        "production_release_allowed": 0,
        "lifecycle_allowed": 0,
        "priority_conflict_resolution": preflight["priority_conflict_resolution"],
        "decision_counts": dict(validation["decision_counts"]),
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
        "registered_descendant_terminal_or_reuse_coverage": int(validation["registered_descendant_terminal_or_reuse_coverage"]),
        "blocked_uncertain_remaining_without_useful_spec": int(validation["blocked_uncertain_remaining_without_useful_spec"]),
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
        "registered_descendant_terminal_or_reuse_coverage": EXPECTED_PROJECTED_GAIN,
        "blocked_uncertain_remaining_without_useful_spec": EXPECTED_REMAINING,
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
        "projected_gain_from_registered_children": int(notes.get("projected_gain_from_registered_children") or 0),
        "blocked_uncertain_remaining_without_useful_spec": int(notes.get("blocked_uncertain_remaining_without_useful_spec") or 0),
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
        "projected_gain_from_registered_children": EXPECTED_PROJECTED_GAIN,
        "blocked_uncertain_remaining_without_useful_spec": EXPECTED_REMAINING,
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
            "remaining_sublanes": REMAINING_SUBLANES,
            "requires_lifecycle_later": False,
            "requires_apply_later": False,
        }, ensure_ascii=False, sort_keys=True) + "\n")
    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(f"Blocked uncertain global policy catalog registry {mode}\n\n")
        for key in [
            "policies_to_register",
            "review_total",
            "registered_child_found",
            "registered_descendant_terminal_or_reuse_coverage",
            "blocked_uncertain_remaining_without_useful_spec",
            "true_blocked_count",
            "requires_apply_later",
            "requires_lifecycle_later",
            "auto_apply_allowed",
            "production_release_allowed",
            "lifecycle_allowed",
        ]:
            handle.write(f"- {key}: {counts[key]}\n")
        handle.write(f"- macro_lane_router_registered: {bool(preflight['parent_registered'])}\n")
        handle.write(f"- priority: {record['priority']}\n")
        handle.write(f"- priority_conflict_resolution: {preflight['priority_conflict_resolution']}\n")
        handle.write(f"- created: {created}\n")
        handle.write(f"- updated: {updated}\n")
        handle.write("\nRemaining sublanes\n")
        for key, value in REMAINING_SUBLANES.items():
            handle.write(f"- {key}: {value}\n")
        handle.write("\nReligion/culture leftovers\n")
        for key, value in RELIGION_CULTURE_LEFTOVERS.items():
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
    parser = argparse.ArgumentParser(description="Register blocked_uncertain global read-only splitter.")
    parser.add_argument("--blocked-jsonl", required=True, type=Path)
    parser.add_argument("--blocked-spec-json", required=True, type=Path)
    parser.add_argument("--religion-culture-registry-jsonl", required=True, type=Path)
    parser.add_argument("--debug-marker-registry-jsonl", required=True, type=Path)
    parser.add_argument("--gender-perspective-registry-jsonl", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    blocked_samples = sample_rows(read_jsonl(args.blocked_jsonl))
    blocked_spec = read_json(args.blocked_spec_json)
    religion_summary = summary_row(read_jsonl(args.religion_culture_registry_jsonl), "religion/culture registry")
    debug_summary = summary_row(read_jsonl(args.debug_marker_registry_jsonl), "debug marker registry")
    gender_summary = summary_row(read_jsonl(args.gender_perspective_registry_jsonl), "gender perspective registry")
    with connect_readonly() as ro_conn:
        preflight = registry_preflight(ro_conn)
    validate_preflight(preflight)
    validation = validate_inputs(
        blocked_spec=blocked_spec,
        blocked_samples=blocked_samples,
        religion_summary=religion_summary,
        debug_summary=debug_summary,
        gender_summary=gender_summary,
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
    print(f"macro_lane_router_registered: {bool(preflight['parent_registered'])}")
    print(f"priority: {record['priority']}")
    print(f"priority_conflict_resolution: {preflight['priority_conflict_resolution']}")
    print(f"created: {created}")
    print(f"updated: {updated}")
    print(f"preexisting_agent: {preflight['preexisting_agent']}")
    if registry_validation:
        print(f"registry_validation: {json.dumps(registry_validation, ensure_ascii=False, sort_keys=True)}")


if __name__ == "__main__":
    main()
