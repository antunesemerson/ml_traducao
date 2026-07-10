from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "effect_list_policy_catalog_registry_v1"
EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76

SPLITTERS = {
    "effect_list_multiline_policy": "requirement_effect_router_readonly",
    "effect_list_artifact_activity_policy": "effect_list_multiline_policy",
    "artifact_item_effect_policy": "effect_list_artifact_activity_policy",
    "artifact_item_scope_getter_policy": "artifact_item_effect_policy",
}

TERMINALS = {
    "effect_list_gender_local_player_policy": "effect_list_multiline_policy",
    "effect_list_trait_accolade_policy": "effect_list_multiline_policy",
    "effect_list_script_value_policy": "effect_list_multiline_policy",
    "effect_list_concept_policy": "effect_list_multiline_policy",
    "artifact_activity_gender_local_player_policy": "effect_list_artifact_activity_policy",
    "artifact_activity_script_value_policy": "effect_list_artifact_activity_policy",
}

EXPECTED_ALREADY_REGISTERED = {
    "artifact_activity_gender_local_player_policy",
    "effect_list_concept_policy",
    "effect_list_gender_local_player_policy",
    "effect_list_script_value_policy",
    "effect_list_trait_accolade_policy",
}

EXPECTED_SPEC_ONLY = {
    "artifact_activity_script_value_policy",
    "artifact_item_effect_policy",
    "artifact_item_scope_getter_policy",
    "effect_list_artifact_activity_policy",
    "effect_list_multiline_policy",
}

ALL_POLICIES = set(SPLITTERS) | set(TERMINALS)


def output_paths(apply: bool) -> tuple[Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    suffix = "apply" if apply else "dry_run"
    base = reports_dir / f"{stamp}_effect_list_policy_catalog_registry_{suffix}"
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


def integration_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    summaries = [row for row in rows if row.get("record_type") == "summary"]
    if len(summaries) != 1:
        raise SystemExit(f"expected exactly one integration summary, got {len(summaries)}")
    return summaries[0]


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def existing_registry_rows(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    placeholders = ",".join("?" for _ in ALL_POLICIES)
    rows = conn.execute(
        f"""
        SELECT *
        FROM ml_agent_registry
        WHERE agent_key IN ({placeholders})
        """,
        tuple(sorted(ALL_POLICIES)),
    ).fetchall()
    return {str(row["agent_key"]): dict(row) for row in rows}


def validate_inputs(inventory: dict[str, Any], summary: dict[str, Any], existing: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    if int(summary.get("segment_state_run_id") or 0) != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("summary segment_state_run_id guard failed")
    if int(summary.get("ledger_run_id") or 0) != EXPECTED_LEDGER_RUN_ID:
        raise SystemExit("summary ledger_run_id guard failed")
    expected_summary = {
        "specs_effect_list_loaded": 10,
        "missing_effect_list_specs": 0,
        "invalid_effect_list_specs": 0,
        "terminal_effect_list_policies": 6,
        "splitter_effect_list_policies": 4,
        "registered_effect_list_specs": 5,
        "spec_only_effect_list_specs": 5,
        "pending_segments_analyzed": 11725,
        "segments_with_requirement_effect_route": 10333,
        "segments_with_effect_list_route": 1654,
        "segments_with_spec_effect_list_associated": 1272,
        "segments_with_terminal_effect_list_policy": 1090,
        "segments_with_registered_terminal_effect_list_policy": 1073,
        "segments_with_spec_associated_after": 5784,
        "requires_apply_later": 0,
        "requires_lifecycle_later": 0,
        "auto_apply_allowed": 0,
        "production_release_allowed": 0,
        "lifecycle_allowed": 0,
    }
    for key, value in expected_summary.items():
        if int(summary.get(key) or 0) != value:
            raise SystemExit(f"summary guard failed: {key}={summary.get(key)} expected {value}")
    if summary.get("database_query_only") is not True:
        raise SystemExit("summary database_query_only guard failed")

    effect_items = inventory.get("effect_list_inventory") or []
    if len(effect_items) != 10:
        raise SystemExit(f"inventory effect_list_inventory guard failed: {len(effect_items)}")
    loaded = [item for item in effect_items if item.get("loaded")]
    missing = [item for item in effect_items if item.get("missing")]
    invalid = [item for item in effect_items if item.get("validation_issue")]
    if len(loaded) != 10 or missing or invalid:
        raise SystemExit("inventory loaded/missing/invalid guard failed")
    ids = {str(item.get("policy_id") or "") for item in loaded}
    if ids != ALL_POLICIES:
        raise SystemExit(f"inventory policy allowlist guard failed: {sorted(ids ^ ALL_POLICIES)}")

    registered_from_inventory = set(inventory.get("registered_effect_list_specs") or [])
    spec_only_from_inventory = set(inventory.get("spec_only_effect_list_specs") or [])
    if registered_from_inventory != EXPECTED_ALREADY_REGISTERED:
        raise SystemExit(f"inventory registered guard failed: {sorted(registered_from_inventory)}")
    if spec_only_from_inventory != EXPECTED_SPEC_ONLY:
        raise SystemExit(f"inventory spec-only guard failed: {sorted(spec_only_from_inventory)}")

    existing_keys = set(existing)
    if existing_keys != EXPECTED_ALREADY_REGISTERED:
        raise SystemExit(f"registry preflight guard failed: existing={sorted(existing_keys)} expected={sorted(EXPECTED_ALREADY_REGISTERED)}")
    return sorted(loaded, key=lambda item: str(item.get("policy_id") or ""))


def merged_notes(existing_row: dict[str, Any] | None, item: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
    previous_notes = json.loads(existing_row.get("notes_json") or "{}") if existing_row else {}
    previous_source = previous_notes.get("source")
    notes = dict(previous_notes)
    notes.update(
        {
            "source": SOURCE,
            "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
            "ledger_run_id": EXPECTED_LEDGER_RUN_ID,
            "catalog_specs_loaded": 10,
            "catalog_terminal_policies": 6,
            "catalog_splitter_policies": 4,
            "segments_with_effect_list_route": 1654,
            "segments_with_spec_effect_list_associated": 1272,
            "segments_with_terminal_effect_list_policy": 1090,
            "segments_with_registered_terminal_effect_list_policy": 1073,
            "segments_with_spec_associated_after": 5784,
            "auto_apply_allowed": 0,
            "production_release_allowed": 0,
            "lifecycle_allowed": 0,
            "read_only_component": True,
            "effect_list_package_component": True,
            "network_ready_after_registration": True,
            "policy_parent": item.get("parent_policy"),
            "catalog_role": item.get("catalog_role"),
            "created_for": item.get("created_for"),
            "terminal_policy": bool(item.get("terminal_policy")),
            "fallback_stage": item.get("fallback_stage") or "",
            "spec_path": item.get("path"),
            "previous_registry_source": previous_source,
            "requires_apply_later": int(summary.get("requires_apply_later") or 0),
            "requires_lifecycle_later": int(summary.get("requires_lifecycle_later") or 0),
        }
    )
    return notes


def planned_records(items: list[dict[str, Any]], summary: dict[str, Any], existing: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for index, item in enumerate(items, 1):
        policy_id = str(item["policy_id"])
        terminal = policy_id in TERMINALS
        parent = TERMINALS.get(policy_id) if terminal else SPLITTERS.get(policy_id)
        if parent != item.get("parent_policy"):
            raise SystemExit(f"parent guard failed for {policy_id}: {item.get('parent_policy')} expected {parent}")
        records.append(
            {
                "agent_key": policy_id,
                "agent_type": "symbolic_subpolicy" if terminal else "subcoordinator",
                "parent_agent_key": parent,
                "status": "active",
                "operational_state": "dry_run" if terminal else "shadow",
                "decision_role": "terminal_guard" if terminal else "route_and_split",
                "scope_group": "requirement_effect_router",
                "scope_description": f"Read-only effect-list catalog policy {policy_id} from {parent}.",
                "dashboard_group": "Issue Network",
                "priority": 35 + index,
                "notes_json": merged_notes(existing.get(policy_id), item, summary),
            }
        )
    return records


def dry_run_counts(records: list[dict[str, Any]], existing: dict[str, dict[str, Any]]) -> dict[str, int]:
    notes = records[0]["notes_json"]
    return {
        "policies_to_register": len(records),
        "terminal_policies": sum(1 for record in records if record["agent_type"] == "symbolic_subpolicy"),
        "splitter_policies": sum(1 for record in records if record["agent_type"] == "subcoordinator"),
        "already_registered": len(existing),
        "spec_only_to_create": len([record for record in records if record["agent_key"] not in existing]),
        "missing_specs": 0,
        "invalid_specs": 0,
        "requires_apply_later": int(notes["requires_apply_later"]),
        "requires_lifecycle_later": int(notes["requires_lifecycle_later"]),
        "auto_apply_allowed": int(notes["auto_apply_allowed"]),
        "production_release_allowed": int(notes["production_release_allowed"]),
        "lifecycle_allowed": int(notes["lifecycle_allowed"]),
    }


def assert_apply_guards(counts: dict[str, int]) -> None:
    expected = {
        "policies_to_register": 10,
        "terminal_policies": 6,
        "splitter_policies": 4,
        "already_registered": 5,
        "spec_only_to_create": 5,
        "missing_specs": 0,
        "invalid_specs": 0,
        "requires_apply_later": 0,
        "requires_lifecycle_later": 0,
        "auto_apply_allowed": 0,
        "production_release_allowed": 0,
        "lifecycle_allowed": 0,
    }
    for key, value in expected.items():
        if counts.get(key) != value:
            raise SystemExit(f"apply guard failed: {key}={counts.get(key)} expected {value}")


def upsert_records(conn: sqlite3.Connection, records: list[dict[str, Any]]) -> tuple[int, int]:
    now = db.utc_now()
    created = 0
    updated = 0
    for record in records:
        exists = conn.execute("SELECT 1 FROM ml_agent_registry WHERE agent_key = ?", (record["agent_key"],)).fetchone()
        created += 0 if exists else 1
        updated += 1 if exists else 0
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
    placeholders = ",".join("?" for _ in ALL_POLICIES)
    rows = conn.execute(
        f"""
        SELECT agent_key, agent_type, parent_agent_key, status, operational_state,
               decision_role, scope_group, dashboard_group, notes_json
        FROM ml_agent_registry
        WHERE agent_key IN ({placeholders})
        """,
        tuple(sorted(ALL_POLICIES)),
    ).fetchall()
    by_key = {str(row["agent_key"]): dict(row) for row in rows}
    missing = sorted(ALL_POLICIES - set(by_key))
    notes = {key: json.loads(row.get("notes_json") or "{}") for key, row in by_key.items()}
    return {
        "rows_found": len(rows),
        "missing_keys": missing,
        "terminal_dry_run": sum(1 for key in TERMINALS if by_key.get(key, {}).get("operational_state") == "dry_run"),
        "splitter_shadow": sum(1 for key in SPLITTERS if by_key.get(key, {}).get("operational_state") == "shadow"),
        "all_active": all(row.get("status") == "active" for row in by_key.values()),
        "scope_group_ok": all(row.get("scope_group") == "requirement_effect_router" for row in by_key.values()),
        "dashboard_group_ok": all(row.get("dashboard_group") == "Issue Network" for row in by_key.values()),
        "auto_apply_allowed_sum": sum(int(payload.get("auto_apply_allowed") or 0) for payload in notes.values()),
        "production_release_allowed_sum": sum(int(payload.get("production_release_allowed") or 0) for payload in notes.values()),
        "lifecycle_allowed_sum": sum(int(payload.get("lifecycle_allowed") or 0) for payload in notes.values()),
        "source_count": sum(1 for payload in notes.values() if payload.get("source") == SOURCE),
    }


def write_reports(
    *,
    apply: bool,
    records: list[dict[str, Any]],
    counts: dict[str, int],
    registry_validation: dict[str, Any] | None,
    created: int,
    updated: int,
) -> tuple[Path, Path]:
    txt_path, jsonl_path = output_paths(apply)
    mode = "apply" if apply else "dry_run"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(
            json.dumps(
                {
                    "record_type": "summary",
                    "mode": mode,
                    **counts,
                    "created": created,
                    "updated": updated,
                    "registry_validation": registry_validation or {},
                    "requires_lifecycle_later": False,
                    "requires_apply_later": False,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )
        for record in records:
            handle.write(
                json.dumps(
                    {
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
                        "catalog_role": record["notes_json"].get("catalog_role"),
                        "terminal_policy": record["notes_json"].get("terminal_policy"),
                        "requires_lifecycle_later": False,
                        "requires_apply_later": False,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )
    type_counts = Counter(record["agent_type"] for record in records)
    state_counts = Counter(record["operational_state"] for record in records)
    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(f"Effect-list policy catalog registry {mode}\n\n")
        for key in [
            "policies_to_register",
            "terminal_policies",
            "splitter_policies",
            "already_registered",
            "spec_only_to_create",
            "missing_specs",
            "invalid_specs",
            "requires_apply_later",
            "requires_lifecycle_later",
            "auto_apply_allowed",
            "production_release_allowed",
            "lifecycle_allowed",
        ]:
            handle.write(f"- {key}: {counts[key]}\n")
        handle.write(f"- created: {created}\n")
        handle.write(f"- updated: {updated}\n")
        handle.write(f"- agent_type_counts: {dict(type_counts)}\n")
        handle.write(f"- operational_state_counts: {dict(state_counts)}\n")
        if registry_validation:
            handle.write(f"- registry_validation: {json.dumps(registry_validation, ensure_ascii=False, sort_keys=True)}\n")
        handle.write("\nAgents\n")
        for record in records:
            handle.write(f"- {record['agent_key']} [{record['agent_type']}/{record['operational_state']}/{record['decision_role']}]\n")
    return txt_path, jsonl_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Register the effect-list catalog package in ml_agent_registry.")
    parser.add_argument("--inventory-json", required=True, type=Path)
    parser.add_argument("--integration-jsonl", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    inventory = read_json(args.inventory_json)
    summary = integration_summary(read_jsonl(args.integration_jsonl))
    with connect_readonly() as ro_conn:
        existing = existing_registry_rows(ro_conn)
    items = validate_inputs(inventory, summary, existing)
    records = planned_records(items, summary, existing)
    counts = dry_run_counts(records, existing)
    assert_apply_guards(counts)

    created = 0
    updated = 0
    registry_validation = None
    if args.apply:
        settings = db.load_settings()
        with db.connect(settings) as conn:
            created, updated = upsert_records(conn, records)
            conn.commit()
            registry_validation = validate_registry(conn)
    txt_path, jsonl_path = write_reports(
        apply=args.apply,
        records=records,
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
    if registry_validation:
        print(f"registry_validation: {json.dumps(registry_validation, ensure_ascii=False, sort_keys=True)}")


if __name__ == "__main__":
    main()
