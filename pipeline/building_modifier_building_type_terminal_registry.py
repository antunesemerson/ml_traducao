from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "building_modifier_building_type_terminal_registry_v1"
AGENT_KEY = "building_modifier_building_type_policy"
PARENT_AGENT_KEY = "building_modifier_effect_policy"
EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76
EXPECTED_REGISTERED_AGENTS_BEFORE = 219
EXPECTED_REVIEW_TOTAL = 68
EXPECTED_TERMINAL_MINIMUM = 66
SOURCE_DECISION = "needs_building_modifier_building_type_policy"
PRIMARY_ROUTE = "building_modifier_effect_policy"
SCOPE_DESCRIPTION = (
    "Read-only terminal guard for building modifier building-type routes; recognizes stable building "
    "type surfaces while preserving parser escape and holding-policy leftovers for later routing."
)


def output_paths(apply: bool) -> tuple[Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    suffix = "apply" if apply else "dry_run"
    base = reports_dir / f"{stamp}_building_modifier_building_type_terminal_registry_{suffix}"
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


def registry_preflight(conn: sqlite3.Connection) -> tuple[bool, bool, int]:
    registry_count = int(conn.execute("SELECT COUNT(*) AS c FROM ml_agent_registry").fetchone()["c"] or 0)
    parent = conn.execute(
        """
        SELECT 1
        FROM ml_agent_registry
        WHERE agent_key = ?
          AND status = 'active'
        """,
        (PARENT_AGENT_KEY,),
    ).fetchone()
    exists = conn.execute("SELECT 1 FROM ml_agent_registry WHERE agent_key = ?", (AGENT_KEY,)).fetchone() is not None
    return parent is not None, exists, registry_count


def validate_inputs(
    *,
    spec: dict[str, Any],
    summary: dict[str, Any],
    samples: list[dict[str, Any]],
    parent_found: bool,
    registry_count: int,
) -> dict[str, Any]:
    if registry_count != EXPECTED_REGISTERED_AGENTS_BEFORE:
        raise SystemExit(f"registry count guard failed: {registry_count} expected {EXPECTED_REGISTERED_AGENTS_BEFORE}")
    if not parent_found:
        raise SystemExit(f"parent policy not active in registry: {PARENT_AGENT_KEY}")
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
    expected_summary = {
        "total_reviewed": EXPECTED_REVIEW_TOTAL,
        "pending_no_run_400": EXPECTED_REVIEW_TOTAL,
        "ready_lifecycle_future": 0,
        "apply_candidates_future": 0,
    }
    for key, value in expected_summary.items():
        if int(summary.get(key) or 0) != value:
            raise SystemExit(f"summary guard failed: {key}={summary.get(key)} expected {value}")
    if summary.get("requires_apply_later") is not False:
        raise SystemExit("summary requires_apply_later guard failed")
    if summary.get("requires_lifecycle_later") is not False:
        raise SystemExit("summary requires_lifecycle_later guard failed")

    source_mismatch = sum(1 for row in samples if row.get("source_decision") != SOURCE_DECISION)
    route_mismatch = sum(1 for row in samples if row.get("primary_route") != PRIMARY_ROUTE)
    if source_mismatch:
        raise SystemExit(f"source_decision guard failed: {source_mismatch}")
    if route_mismatch:
        raise SystemExit(f"primary_route guard failed: {route_mismatch}")

    requires_apply = sum(1 for row in samples if row.get("requires_apply_later") is True)
    requires_lifecycle = sum(1 for row in samples if row.get("requires_lifecycle_later") is True)
    if requires_apply:
        raise SystemExit(f"requires_apply_later guard failed: {requires_apply}")
    if requires_lifecycle:
        raise SystemExit(f"requires_lifecycle_later guard failed: {requires_lifecycle}")

    decision_counts = Counter(str(row.get("building_type_decision") or "") for row in samples)
    terminal_count = int(decision_counts.get("building_type_terminal_policy", 0))
    terminal_total = sum(count for decision, count in decision_counts.items() if decision.startswith("building_type_terminal"))
    if terminal_count < EXPECTED_TERMINAL_MINIMUM:
        raise SystemExit(f"building_type_terminal_policy guard failed: {terminal_count}")
    if terminal_total < EXPECTED_TERMINAL_MINIMUM:
        raise SystemExit(f"terminal_policies guard failed: {terminal_total}")
    return {
        "review_total": len(samples),
        "terminal_policies": terminal_total,
        "building_type_terminal_policy": terminal_count,
        "requires_apply_later": requires_apply,
        "requires_lifecycle_later": requires_lifecycle,
        "parent_policy_found": 1 if parent_found else 0,
        "decision_counts": decision_counts,
    }


def planned_record(validation: dict[str, Any]) -> dict[str, Any]:
    decision_counts = dict(validation["decision_counts"])
    notes = {
        "source": SOURCE,
        "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
        "ledger_run_id": EXPECTED_LEDGER_RUN_ID,
        "review_total": int(validation["review_total"]),
        "terminal_policies": int(validation["terminal_policies"]),
        "building_type_terminal_policy": int(validation["building_type_terminal_policy"]),
        "requires_apply_later": 0,
        "requires_lifecycle_later": 0,
        "auto_apply_allowed": 0,
        "production_release_allowed": 0,
        "lifecycle_allowed": 0,
        "read_only_component": True,
        "terminal_policy": True,
        "terminal_guard": True,
        "parent_policy": PARENT_AGENT_KEY,
        "source_decision": SOURCE_DECISION,
        "primary_route": PRIMARY_ROUTE,
        "leftovers": {
            "needs_building_type_dynamic_parser_escape": int(decision_counts.get("needs_building_type_dynamic_parser_escape", 0)),
            "needs_building_type_holding_policy": int(decision_counts.get("needs_building_type_holding_policy", 0)),
        },
        "policy_shape": "terminal_guard",
        "decision_counts": decision_counts,
    }
    return {
        "agent_key": AGENT_KEY,
        "agent_type": "symbolic_subpolicy",
        "parent_agent_key": PARENT_AGENT_KEY,
        "status": "active",
        "operational_state": "dry_run",
        "decision_role": "terminal_guard",
        "scope_group": "requirement_effect_router",
        "scope_description": SCOPE_DESCRIPTION,
        "dashboard_group": "Issue Network",
        "priority": 36,
        "notes_json": notes,
    }


def dry_run_counts(validation: dict[str, Any]) -> dict[str, int]:
    notes = planned_record(validation)["notes_json"]
    return {
        "policies_to_register": 1,
        "review_total": int(validation["review_total"]),
        "terminal_policies": int(validation["terminal_policies"]),
        "building_type_terminal_policy": int(validation["building_type_terminal_policy"]),
        "parent_policy_found": int(validation["parent_policy_found"]),
        "requires_apply_later": int(validation["requires_apply_later"]),
        "requires_lifecycle_later": int(validation["requires_lifecycle_later"]),
        "auto_apply_allowed": int(notes["auto_apply_allowed"]),
        "production_release_allowed": int(notes["production_release_allowed"]),
        "lifecycle_allowed": int(notes["lifecycle_allowed"]),
    }


def assert_apply_guards(counts: dict[str, int]) -> None:
    expected = {
        "policies_to_register": 1,
        "review_total": 68,
        "parent_policy_found": 1,
        "requires_apply_later": 0,
        "requires_lifecycle_later": 0,
        "auto_apply_allowed": 0,
        "production_release_allowed": 0,
        "lifecycle_allowed": 0,
    }
    for key, value in expected.items():
        if counts.get(key) != value:
            raise SystemExit(f"apply guard failed: {key}={counts.get(key)} expected {value}")
    if counts.get("terminal_policies", 0) < EXPECTED_TERMINAL_MINIMUM:
        raise SystemExit("apply guard failed: terminal_policies")
    if counts.get("building_type_terminal_policy", 0) < EXPECTED_TERMINAL_MINIMUM:
        raise SystemExit("apply guard failed: building_type_terminal_policy")


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
        "source": notes.get("source"),
        "review_total": int(notes.get("review_total") or 0),
        "terminal_policies": int(notes.get("terminal_policies") or 0),
        "building_type_terminal_policy": int(notes.get("building_type_terminal_policy") or 0),
    }


def write_reports(
    *,
    apply: bool,
    record: dict[str, Any],
    counts: dict[str, int],
    registry_validation: dict[str, Any] | None,
    created: int,
    updated: int,
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
            "terminal_policy": record["notes_json"].get("terminal_policy", False),
            "terminal_guard": record["notes_json"].get("terminal_guard", False),
            "read_only_component": record["notes_json"].get("read_only_component", False),
            "requires_lifecycle_later": False,
            "requires_apply_later": False,
        }, ensure_ascii=False, sort_keys=True) + "\n")
    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(f"Building modifier building-type terminal registry {mode}\n\n")
        for key in [
            "policies_to_register",
            "review_total",
            "terminal_policies",
            "building_type_terminal_policy",
            "parent_policy_found",
            "requires_apply_later",
            "requires_lifecycle_later",
            "auto_apply_allowed",
            "production_release_allowed",
            "lifecycle_allowed",
        ]:
            handle.write(f"- {key}: {counts[key]}\n")
        handle.write(f"- created: {created}\n")
        handle.write(f"- updated: {updated}\n")
        if registry_validation:
            handle.write(f"- registry_validation: {json.dumps(registry_validation, ensure_ascii=False, sort_keys=True)}\n")
        handle.write("\nAgent\n")
        handle.write(f"- {record['agent_key']} [{record['agent_type']}/{record['operational_state']}/{record['decision_role']}]\n")
    return txt_path, jsonl_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Register building modifier building-type terminal read-only policy.")
    parser.add_argument("--review-jsonl", required=True, type=Path)
    parser.add_argument("--spec-json", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    spec = read_json(args.spec_json)
    rows = read_jsonl(args.review_jsonl)
    summary = review_summary(rows)
    samples = sample_rows(rows)
    with connect_readonly() as ro_conn:
        parent_found, exists, registry_count = registry_preflight(ro_conn)
    validation = validate_inputs(
        spec=spec,
        summary=summary,
        samples=samples,
        parent_found=parent_found,
        registry_count=registry_count,
    )
    record = planned_record(validation)
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
    )
    print(f"txt: {txt_path}")
    print(f"jsonl: {jsonl_path}")
    print(f"mode: {'apply' if args.apply else 'dry_run'}")
    for key, value in counts.items():
        print(f"{key}: {value}")
    print(f"created: {created}")
    print(f"updated: {updated}")
    print(f"preexisting_agent: {exists}")
    if registry_validation:
        print(f"registry_validation: {json.dumps(registry_validation, ensure_ascii=False, sort_keys=True)}")


if __name__ == "__main__":
    main()
