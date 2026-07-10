from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "building_modifier_effect_policy_catalog_registry_v1"
AGENT_KEY = "building_modifier_effect_policy"
PARENT_AGENT_KEY = "requirement_effect_router_readonly"
EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76
EXPECTED_REGISTERED_AGENTS_BEFORE = 218
EXPECTED_UNIVERSE = 866
EXPECTED_REVIEW_TOTAL = 240
EXPECTED_REUSE_MINIMUM = 139
EXPECTED_TERMINAL_POLICIES = 10
EXPECTED_POLICY_SHAPE = "splitter_candidate"
REUSED_POLICIES = [
    "effect_list_concept_policy",
    "effect_list_script_value_policy",
    "artifact_activity_effect_policy",
]
SCOPE_DESCRIPTION = (
    "Read-only splitter for building/modifier effect routes; delegates to effect-list concept, "
    "effect-list ScriptValue and artifact/activity effect policies while preserving building-type "
    "sublane for later review."
)


def output_paths(apply: bool) -> tuple[Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    suffix = "apply" if apply else "dry_run"
    base = reports_dir / f"{stamp}_building_modifier_effect_policy_catalog_registry_{suffix}"
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


def registry_preflight(conn: sqlite3.Connection) -> tuple[set[str], bool, int]:
    registry_count = int(conn.execute("SELECT COUNT(*) AS c FROM ml_agent_registry").fetchone()["c"] or 0)
    placeholders = ",".join("?" for _ in REUSED_POLICIES)
    rows = conn.execute(
        f"""
        SELECT agent_key
        FROM ml_agent_registry
        WHERE agent_key IN ({placeholders})
          AND status = 'active'
        """,
        tuple(REUSED_POLICIES),
    ).fetchall()
    active_reused = {str(row["agent_key"]) for row in rows}
    exists = conn.execute("SELECT 1 FROM ml_agent_registry WHERE agent_key = ?", (AGENT_KEY,)).fetchone() is not None
    return active_reused, exists, registry_count


def catalog_spec_keys(inventory: dict[str, Any]) -> set[str]:
    items = inventory.get("effect_list_inventory") or []
    return {str(item.get("policy_id") or "") for item in items if item.get("loaded") and not item.get("validation_issue")}


def validate_inputs(
    *,
    spec: dict[str, Any],
    inventory: dict[str, Any],
    summary: dict[str, Any],
    samples: list[dict[str, Any]],
    active_reused: set[str],
    registry_count: int,
) -> dict[str, Any]:
    if registry_count != EXPECTED_REGISTERED_AGENTS_BEFORE:
        raise SystemExit(f"registry count guard failed: {registry_count} expected {EXPECTED_REGISTERED_AGENTS_BEFORE}")
    if spec.get("policy_id") != AGENT_KEY:
        raise SystemExit(f"spec policy_id guard failed: {spec.get('policy_id')}")
    if spec.get("parent_policy") != PARENT_AGENT_KEY:
        raise SystemExit(f"spec parent_policy guard failed: {spec.get('parent_policy')}")
    if int(spec.get("segment_state_run_id") or 0) != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("spec segment_state_run_id guard failed")
    if int(spec.get("ledger_run_id") or 0) != EXPECTED_LEDGER_RUN_ID:
        raise SystemExit("spec ledger_run_id guard failed")
    if spec.get("policy_shape") != EXPECTED_POLICY_SHAPE:
        raise SystemExit(f"spec policy_shape guard failed: {spec.get('policy_shape')}")

    if len(samples) != EXPECTED_REVIEW_TOTAL:
        raise SystemExit(f"review_total guard failed: {len(samples)} expected {EXPECTED_REVIEW_TOTAL}")
    expected_summary = {
        "universe_estimated": EXPECTED_UNIVERSE,
        "total_reviewed": EXPECTED_REVIEW_TOTAL,
        "pending_no_run_400": EXPECTED_REVIEW_TOTAL,
        "terminal_policy_count": EXPECTED_TERMINAL_POLICIES,
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

    requires_apply = sum(1 for row in samples if row.get("requires_apply_later") is True)
    requires_lifecycle = sum(1 for row in samples if row.get("requires_lifecycle_later") is True)
    if requires_apply:
        raise SystemExit(f"requires_apply_later guard failed: {requires_apply}")
    if requires_lifecycle:
        raise SystemExit(f"requires_lifecycle_later guard failed: {requires_lifecycle}")

    reuse_count = sum(1 for row in samples if str(row.get("building_modifier_decision") or "").startswith("building_modifier_reuse_"))
    if reuse_count < EXPECTED_REUSE_MINIMUM:
        raise SystemExit(f"reuse guard failed: {reuse_count} expected >= {EXPECTED_REUSE_MINIMUM}")

    catalog_keys = catalog_spec_keys(inventory)
    missing_reused = sorted(key for key in REUSED_POLICIES if key not in active_reused and key not in catalog_keys)
    missing_catalog = sorted(key for key in REUSED_POLICIES if key not in active_reused and key not in catalog_keys)
    if missing_reused:
        raise SystemExit(f"missing reused policies or catalog specs: {missing_reused}")

    matched_counts = Counter(str(row.get("matched_registered_policy") or row.get("matched_catalog_spec") or "") for row in samples)
    return {
        "review_total": len(samples),
        "reuse_cataloged_policies": reuse_count,
        "terminal_policies": int(summary.get("terminal_policy_count") or 0),
        "requires_apply_later": requires_apply,
        "requires_lifecycle_later": requires_lifecycle,
        "missing_reused_policy_keys": len(missing_reused),
        "missing_catalog_spec_keys": len(missing_catalog),
        "decision_counts": Counter(str(row.get("building_modifier_decision") or "") for row in samples),
        "matched_policy_counts": matched_counts,
    }


def planned_record(validation: dict[str, Any]) -> dict[str, Any]:
    notes = {
        "source": SOURCE,
        "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
        "ledger_run_id": EXPECTED_LEDGER_RUN_ID,
        "universe_estimated": EXPECTED_UNIVERSE,
        "review_total": int(validation["review_total"]),
        "reuse_cataloged_policies": int(validation["reuse_cataloged_policies"]),
        "terminal_policies": int(validation["terminal_policies"]),
        "policy_shape": EXPECTED_POLICY_SHAPE,
        "requires_apply_later": 0,
        "requires_lifecycle_later": 0,
        "auto_apply_allowed": 0,
        "production_release_allowed": 0,
        "lifecycle_allowed": 0,
        "read_only_component": True,
        "terminal_policy": False,
        "parent_policy": PARENT_AGENT_KEY,
        "reused_policies": REUSED_POLICIES,
        "dominant_pending_sublane": "needs_building_modifier_building_type_policy",
        "catalog_reuse_ratio": round(int(validation["reuse_cataloged_policies"]) / int(validation["review_total"]), 4),
        "decision_counts": dict(validation["decision_counts"]),
        "matched_policy_counts": dict(validation["matched_policy_counts"]),
    }
    return {
        "agent_key": AGENT_KEY,
        "agent_type": "subcoordinator",
        "parent_agent_key": PARENT_AGENT_KEY,
        "status": "active",
        "operational_state": "shadow",
        "decision_role": "route_and_split",
        "scope_group": "requirement_effect_router",
        "scope_description": SCOPE_DESCRIPTION,
        "dashboard_group": "Issue Network",
        "priority": 37,
        "notes_json": notes,
    }


def dry_run_counts(validation: dict[str, Any]) -> dict[str, int]:
    notes = planned_record(validation)["notes_json"]
    return {
        "policies_to_register": 1,
        "review_total": int(validation["review_total"]),
        "reuse_cataloged_policies": int(validation["reuse_cataloged_policies"]),
        "missing_reused_policy_keys": int(validation["missing_reused_policy_keys"]),
        "missing_catalog_spec_keys": int(validation["missing_catalog_spec_keys"]),
        "requires_apply_later": int(validation["requires_apply_later"]),
        "requires_lifecycle_later": int(validation["requires_lifecycle_later"]),
        "auto_apply_allowed": int(notes["auto_apply_allowed"]),
        "production_release_allowed": int(notes["production_release_allowed"]),
        "lifecycle_allowed": int(notes["lifecycle_allowed"]),
    }


def assert_apply_guards(counts: dict[str, int]) -> None:
    expected = {
        "policies_to_register": 1,
        "review_total": 240,
        "missing_reused_policy_keys": 0,
        "missing_catalog_spec_keys": 0,
        "requires_apply_later": 0,
        "requires_lifecycle_later": 0,
        "auto_apply_allowed": 0,
        "production_release_allowed": 0,
        "lifecycle_allowed": 0,
    }
    for key, value in expected.items():
        if counts.get(key) != value:
            raise SystemExit(f"apply guard failed: {key}={counts.get(key)} expected {value}")
    if counts.get("reuse_cataloged_policies", 0) < EXPECTED_REUSE_MINIMUM:
        raise SystemExit("apply guard failed: reuse_cataloged_policies")


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
        "reuse_cataloged_policies": int(notes.get("reuse_cataloged_policies") or 0),
        "policy_shape": notes.get("policy_shape"),
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
            "read_only_component": record["notes_json"].get("read_only_component", False),
            "requires_lifecycle_later": False,
            "requires_apply_later": False,
        }, ensure_ascii=False, sort_keys=True) + "\n")
    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(f"Building/modifier effect policy catalog registry {mode}\n\n")
        for key in [
            "policies_to_register",
            "review_total",
            "reuse_cataloged_policies",
            "missing_reused_policy_keys",
            "missing_catalog_spec_keys",
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
    parser = argparse.ArgumentParser(description="Register building/modifier effect policy as read-only catalog splitter.")
    parser.add_argument("--review-jsonl", required=True, type=Path)
    parser.add_argument("--spec-json", required=True, type=Path)
    parser.add_argument("--effect-list-inventory-json", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    spec = read_json(args.spec_json)
    inventory = read_json(args.effect_list_inventory_json)
    rows = read_jsonl(args.review_jsonl)
    summary = review_summary(rows)
    samples = sample_rows(rows)
    with connect_readonly() as ro_conn:
        active_reused, exists, registry_count = registry_preflight(ro_conn)
    validation = validate_inputs(
        spec=spec,
        inventory=inventory,
        summary=summary,
        samples=samples,
        active_reused=active_reused,
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
