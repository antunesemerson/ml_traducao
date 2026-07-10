from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "requirement_effect_router_component_registry_v1"
PARENT_AGENT_KEY = "requirement_effect_router_readonly"
EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76

PARENT_DESCRIPTION = (
    "Read-only requirement/effect router with terminal policy catalog; routes tooltip, scope/getter, "
    "gender/local-player, ScriptValue, concept and name/nickname requirement surfaces before generic dynamic parser."
)


def output_paths(apply: bool) -> tuple[Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    suffix = "apply" if apply else "dry_run"
    base = reports_dir / f"{stamp}_requirement_effect_router_component_registry_{suffix}"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl")


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
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


def validate_inputs(inventory: dict[str, Any], summary: dict[str, Any]) -> None:
    expected = {
        "loaded": 18,
        "missing": 0,
        "invalid": 0,
        "terminal": 9,
        "splitter": 9,
    }
    for key, value in expected.items():
        if int(inventory.get(key) or 0) != value:
            raise SystemExit(f"inventory guard failed: {key}={inventory.get(key)} expected {value}")
    if int(summary.get("specs_loaded") or 0) != 18:
        raise SystemExit("summary guard failed: specs_loaded")
    if int(summary.get("specs_missing") or 0) != 0:
        raise SystemExit("summary guard failed: specs_missing")
    if int(summary.get("specs_invalid") or 0) != 0:
        raise SystemExit("summary guard failed: specs_invalid")
    if int(summary.get("terminal_policies") or 0) != 9:
        raise SystemExit("summary guard failed: terminal_policies")
    if int(summary.get("splitter_policies") or 0) != 9:
        raise SystemExit("summary guard failed: splitter_policies")
    if any(row.get("requires_apply_later") is True or row.get("requires_lifecycle_later") is True for row in [summary]):
        raise SystemExit("summary guard failed: future apply/lifecycle flag")


def loaded_policy_items(inventory: dict[str, Any]) -> list[dict[str, Any]]:
    items = [item for item in inventory.get("inventory", []) if item.get("loaded")]
    if len(items) != 18:
        raise SystemExit(f"expected 18 loaded policy specs, got {len(items)}")
    return sorted(items, key=lambda item: (str(item.get("parent_policy") or ""), str(item.get("policy_id") or "")))


def common_notes(inventory: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": SOURCE,
        "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
        "ledger_run_id": EXPECTED_LEDGER_RUN_ID,
        "catalog_specs_loaded": 18,
        "catalog_terminal_policies": 9,
        "catalog_splitter_policies": 9,
        "segments_with_spec_associated": int(summary.get("segments_with_spec_associated") or 0),
        "segments_with_terminal_policy": int(summary.get("segments_with_terminal_policy") or 0),
        "auto_apply_allowed": 0,
        "production_release_allowed": 0,
        "lifecycle_allowed": 0,
        "read_only_component": True,
        "network_ready_after_registration": True,
    }


def planned_records(inventory: dict[str, Any], summary: dict[str, Any]) -> list[dict[str, Any]]:
    notes_base = common_notes(inventory, summary)
    records = [
        {
            "agent_key": PARENT_AGENT_KEY,
            "agent_type": "subcoordinator",
            "parent_agent_key": "coordinator_ensemble_v1",
            "status": "active",
            "operational_state": "dry_run",
            "decision_role": "route_and_arbitrate",
            "scope_group": "requirement_effect_router",
            "scope_description": PARENT_DESCRIPTION,
            "dashboard_group": "Issue Network",
            "priority": 24,
            "notes_json": {**notes_base, "component_role": "parent_router"},
        }
    ]
    for offset, item in enumerate(loaded_policy_items(inventory), 1):
        terminal = bool(item.get("terminal_policy"))
        records.append(
            {
                "agent_key": item["policy_id"],
                "agent_type": "symbolic_subpolicy" if terminal else "subcoordinator",
                "parent_agent_key": PARENT_AGENT_KEY,
                "status": "active",
                "operational_state": "dry_run" if terminal else "shadow",
                "decision_role": "terminal_guard" if terminal else "route_and_split",
                "scope_group": "requirement_effect_router",
                "scope_description": f"Read-only catalog policy {item['policy_id']} from {item['parent_policy']}.",
                "dashboard_group": "Issue Network",
                "priority": 24 + offset,
                "notes_json": {
                    **notes_base,
                    "component_role": "terminal_policy" if terminal else "splitter_policy",
                    "policy_parent": item.get("parent_policy"),
                    "created_for": item.get("created_for"),
                    "promotion_gate": "read_only_component_only_no_apply_no_lifecycle",
                    "fallback_stage": "",
                    "terminal_policy": terminal,
                    "spec_path": item.get("path"),
                },
            }
        )
    return records


def dry_run_counts(records: list[dict[str, Any]], inventory: dict[str, Any], summary: dict[str, Any]) -> dict[str, int]:
    return {
        "parent_to_register": 1,
        "policies_to_register": len(records) - 1,
        "total_to_register": len(records),
        "terminal_policies": sum(1 for record in records[1:] if record["agent_type"] == "symbolic_subpolicy"),
        "splitter_policies": sum(1 for record in records[1:] if record["agent_type"] == "subcoordinator"),
        "missing_specs": int(inventory.get("missing") or 0),
        "invalid_specs": int(inventory.get("invalid") or 0),
        "requires_apply_later": 1 if summary.get("requires_apply_later") is True else 0,
        "requires_lifecycle_later": 1 if summary.get("requires_lifecycle_later") is True else 0,
    }


def assert_apply_guards(counts: dict[str, int]) -> None:
    expected = {
        "parent_to_register": 1,
        "policies_to_register": 18,
        "total_to_register": 19,
        "terminal_policies": 9,
        "splitter_policies": 9,
        "missing_specs": 0,
        "invalid_specs": 0,
        "requires_apply_later": 0,
        "requires_lifecycle_later": 0,
    }
    for key, value in expected.items():
        if counts.get(key) != value:
            raise SystemExit(f"apply guard failed: {key}={counts.get(key)} expected {value}")


def upsert_records(conn: sqlite3.Connection, records: list[dict[str, Any]]) -> tuple[int, int]:
    now = db.utc_now()
    created = 0
    updated = 0
    for record in records:
        exists = conn.execute(
            "SELECT 1 FROM ml_agent_registry WHERE agent_key = ?",
            (record["agent_key"],),
        ).fetchone()
        if exists:
            updated += 1
        else:
            created += 1
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


def validate_registry(conn: sqlite3.Connection, records: list[dict[str, Any]]) -> dict[str, Any]:
    keys = [record["agent_key"] for record in records]
    placeholders = ",".join("?" for _ in keys)
    rows = conn.execute(
        f"""
        SELECT agent_key, agent_type, status, operational_state, notes_json
        FROM ml_agent_registry
        WHERE agent_key IN ({placeholders})
        """,
        keys,
    ).fetchall()
    by_key = {row["agent_key"]: dict(row) for row in rows}
    missing = [key for key in keys if key not in by_key]
    notes_rows = []
    for key, row in by_key.items():
        notes = json.loads(row.get("notes_json") or "{}")
        notes_rows.append((key, row, notes))
    terminal_dry = sum(
        1
        for key, row, notes in notes_rows
        if key != PARENT_AGENT_KEY and notes.get("terminal_policy") is True and row["operational_state"] == "dry_run"
    )
    splitter_shadow = sum(
        1
        for key, row, notes in notes_rows
        if key != PARENT_AGENT_KEY and notes.get("terminal_policy") is False and row["operational_state"] == "shadow"
    )
    return {
        "rows_found": len(rows),
        "missing_keys": missing,
        "parent_exists": PARENT_AGENT_KEY in by_key,
        "parent_operational_state": by_key.get(PARENT_AGENT_KEY, {}).get("operational_state"),
        "terminal_dry_run": terminal_dry,
        "splitter_shadow": splitter_shadow,
        "all_active": all(row["status"] == "active" for _, row, _ in notes_rows),
        "auto_apply_allowed_sum": sum(int(notes.get("auto_apply_allowed") or 0) for _, _, notes in notes_rows),
        "production_release_allowed_sum": sum(int(notes.get("production_release_allowed") or 0) for _, _, notes in notes_rows),
        "lifecycle_allowed_sum": sum(int(notes.get("lifecycle_allowed") or 0) for _, _, notes in notes_rows),
        "source_count": sum(1 for _, _, notes in notes_rows if notes.get("source") == SOURCE),
    }


def write_reports(
    *,
    apply: bool,
    records: list[dict[str, Any]],
    counts: dict[str, int],
    registry_counts: dict[str, Any] | None,
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
                    "registry_validation": registry_counts or {},
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
                        "priority": record["priority"],
                        "terminal_policy": record["notes_json"].get("terminal_policy", False),
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
        handle.write(f"Requirement/effect router component registry {mode}\n\n")
        for key in [
            "parent_to_register",
            "policies_to_register",
            "total_to_register",
            "terminal_policies",
            "splitter_policies",
            "missing_specs",
            "invalid_specs",
            "requires_apply_later",
            "requires_lifecycle_later",
        ]:
            handle.write(f"- {key}: {counts[key]}\n")
        handle.write(f"- created: {created}\n")
        handle.write(f"- updated: {updated}\n")
        handle.write(f"- agent_type_counts: {dict(type_counts)}\n")
        handle.write(f"- operational_state_counts: {dict(state_counts)}\n")
        if registry_counts:
            handle.write(f"- registry_validation: {json.dumps(registry_counts, ensure_ascii=False, sort_keys=True)}\n")
        handle.write("\nAgents\n")
        for record in records:
            handle.write(f"- {record['agent_key']} [{record['agent_type']}/{record['operational_state']}/{record['decision_role']}]\n")
    return txt_path, jsonl_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Register requirement/effect router read-only component in ml_agent_registry.")
    parser.add_argument("--inventory-json", required=True, type=Path)
    parser.add_argument("--integration-jsonl", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    inventory = read_json(args.inventory_json)
    summary = integration_summary(read_jsonl(args.integration_jsonl))
    validate_inputs(inventory, summary)
    records = planned_records(inventory, summary)
    counts = dry_run_counts(records, inventory, summary)
    assert_apply_guards(counts)

    created = 0
    updated = 0
    registry_counts = None
    if args.apply:
        settings = db.load_settings()
        with db.connect(settings) as conn:
            created, updated = upsert_records(conn, records)
            conn.commit()
            registry_counts = validate_registry(conn, records)
    txt_path, jsonl_path = write_reports(
        apply=args.apply,
        records=records,
        counts=counts,
        registry_counts=registry_counts,
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
    if registry_counts:
        print(f"registry_validation: {json.dumps(registry_counts, ensure_ascii=False, sort_keys=True)}")


if __name__ == "__main__":
    main()
