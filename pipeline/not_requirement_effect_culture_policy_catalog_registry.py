from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "not_requirement_effect_culture_policy_catalog_registry_v1"
AGENT_KEY = "not_requirement_effect_culture_policy"
PARENT_AGENT_KEY = "not_requirement_effect_culture_religion_router"
CHILD_AGENT_KEY = "not_requirement_effect_culture_tradition_heritage_policy"
EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76
EXPECTED_REGISTERED_AGENTS_BEFORE = 226
EXPECTED_REVIEW_TOTAL = 48
EXPECTED_DOMINANT_SUBLANE = "needs_culture_tradition_heritage_policy"
EXPECTED_DOMINANT_SUBLANE_COUNT = 42
SCOPE_DESCRIPTION = (
    "Read-only splitter for not-requirement/effect culture routes; delegates primarily "
    "to tradition/heritage terminal guard while preserving language/ethos, name/location, "
    "culture-title and residual leftovers."
)
PRESERVED_SUBLANES = [
    "culture_terminal_policy_with_domain_guard",
    "needs_culture_language_ethos_policy",
    "needs_culture_name_location_policy",
    "needs_tradition_heritage_culture_title_policy",
    "needs_tradition_heritage_residual_repair",
]


def output_paths(apply: bool) -> tuple[Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    suffix = "apply" if apply else "dry_run"
    base = reports_dir / f"{stamp}_not_requirement_effect_culture_policy_catalog_registry_{suffix}"
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
    probe = conn.execute("PRAGMA query_only").fetchone()
    if int(probe[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def summary_row(rows: list[dict[str, Any]], label: str) -> dict[str, Any]:
    summaries = [row for row in rows if row.get("record_type") == "summary"]
    if len(summaries) != 1:
        raise SystemExit(f"expected exactly one summary in {label}, got {len(summaries)}")
    return summaries[0]


def sample_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    samples = [row for row in rows if row.get("record_type") == "sample_review"]
    ids = [int(row["segment_id"]) for row in samples]
    if len(ids) != len(set(ids)):
        raise SystemExit("duplicate sample segment_id in culture review")
    return samples


def registry_preflight(conn: sqlite3.Connection) -> dict[str, Any]:
    registry_count = int(conn.execute("SELECT COUNT(*) AS c FROM ml_agent_registry").fetchone()["c"] or 0)
    own = conn.execute("SELECT 1 FROM ml_agent_registry WHERE agent_key = ?", (AGENT_KEY,)).fetchone() is not None
    parent = conn.execute("SELECT 1 FROM ml_agent_registry WHERE agent_key = ?", (PARENT_AGENT_KEY,)).fetchone() is not None
    child_row = conn.execute(
        """
        SELECT agent_key, status, operational_state, agent_type, decision_role, parent_agent_key
        FROM ml_agent_registry
        WHERE agent_key = ?
        """,
        (CHILD_AGENT_KEY,),
    ).fetchone()
    return {
        "registry_count": registry_count,
        "preexisting": own,
        "parent_registered": parent,
        "child": dict(child_row) if child_row else None,
    }


def validate_inputs(
    *,
    culture_spec: dict[str, Any],
    culture_summary: dict[str, Any],
    culture_samples: list[dict[str, Any]],
    tradition_apply_summary: dict[str, Any],
    registry_info: dict[str, Any],
) -> dict[str, Any]:
    if int(registry_info["registry_count"]) != EXPECTED_REGISTERED_AGENTS_BEFORE:
        raise SystemExit(
            f"registry count guard failed: {registry_info['registry_count']} expected {EXPECTED_REGISTERED_AGENTS_BEFORE}"
        )
    if culture_spec.get("policy_id") != AGENT_KEY:
        raise SystemExit(f"spec policy_id guard failed: {culture_spec.get('policy_id')}")
    if culture_spec.get("parent_policy") != PARENT_AGENT_KEY:
        raise SystemExit(f"spec parent_policy guard failed: {culture_spec.get('parent_policy')}")
    if int(culture_spec.get("segment_state_run_id") or 0) != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("spec segment_state_run_id guard failed")
    if int(culture_spec.get("ledger_run_id") or 0) != EXPECTED_LEDGER_RUN_ID:
        raise SystemExit("spec ledger_run_id guard failed")
    if len(culture_samples) != EXPECTED_REVIEW_TOTAL:
        raise SystemExit(f"review_total guard failed: {len(culture_samples)} expected {EXPECTED_REVIEW_TOTAL}")
    if int(culture_summary.get("total_reviewed") or 0) != EXPECTED_REVIEW_TOTAL:
        raise SystemExit("summary total_reviewed guard failed")
    if culture_summary.get("requires_apply_later") is not False:
        raise SystemExit("summary requires_apply_later guard failed")
    if culture_summary.get("requires_lifecycle_later") is not False:
        raise SystemExit("summary requires_lifecycle_later guard failed")
    if int(culture_summary.get("ready_lifecycle_future") or 0) != 0:
        raise SystemExit("ready_lifecycle_future guard failed")
    if int(culture_summary.get("apply_candidates_future") or 0) != 0:
        raise SystemExit("apply_candidates_future guard failed")

    decision_counts = Counter(str(row.get("culture_decision") or "") for row in culture_samples)
    dominant_count = int(decision_counts.get(EXPECTED_DOMINANT_SUBLANE, 0))
    if dominant_count != EXPECTED_DOMINANT_SUBLANE_COUNT:
        raise SystemExit(f"dominant sublane guard failed: {dominant_count} expected {EXPECTED_DOMINANT_SUBLANE_COUNT}")
    apply_count = sum(1 for row in culture_samples if row.get("requires_apply_later") is True)
    lifecycle_count = sum(1 for row in culture_samples if row.get("requires_lifecycle_later") is True)
    if apply_count:
        raise SystemExit(f"requires_apply_later guard failed: {apply_count}")
    if lifecycle_count:
        raise SystemExit(f"requires_lifecycle_later guard failed: {lifecycle_count}")

    child = registry_info["child"]
    if not child:
        raise SystemExit(f"registered child missing: {CHILD_AGENT_KEY}")
    expected_child = {
        "status": "active",
        "operational_state": "dry_run",
        "agent_type": "symbolic_subpolicy",
        "decision_role": "terminal_guard",
        "parent_agent_key": AGENT_KEY,
    }
    for key, value in expected_child.items():
        if str(child.get(key) or "") != value:
            raise SystemExit(f"child registry guard failed: {key}={child.get(key)} expected {value}")
    if str(tradition_apply_summary.get("mode") or "") != "apply":
        raise SystemExit("tradition registry apply summary mode guard failed")
    if int(tradition_apply_summary.get("created") or 0) + int(tradition_apply_summary.get("updated") or 0) < 1:
        raise SystemExit("tradition registry apply summary write guard failed")
    if int(tradition_apply_summary.get("terminal_policies") or 0) < 32:
        raise SystemExit("tradition terminal_policies guard failed")
    return {
        "review_total": len(culture_samples),
        "terminal_policies": int(culture_summary.get("terminal_policy_count") or 0),
        "dominant_sublane_count": dominant_count,
        "registered_child_found": 1,
        "requires_apply_later": apply_count,
        "requires_lifecycle_later": lifecycle_count,
        "decision_counts": decision_counts,
    }


def planned_record(validation: dict[str, Any], *, parent_registered: bool) -> dict[str, Any]:
    notes = {
        "source": SOURCE,
        "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
        "ledger_run_id": EXPECTED_LEDGER_RUN_ID,
        "review_total": int(validation["review_total"]),
        "terminal_policies": int(validation["terminal_policies"]),
        "dominant_sublane": EXPECTED_DOMINANT_SUBLANE,
        "dominant_sublane_count": int(validation["dominant_sublane_count"]),
        "registered_child_policies": [CHILD_AGENT_KEY],
        "requires_apply_later": 0,
        "requires_lifecycle_later": 0,
        "auto_apply_allowed": 0,
        "production_release_allowed": 0,
        "lifecycle_allowed": 0,
        "read_only_component": True,
        "terminal_policy": False,
        "parent_policy": PARENT_AGENT_KEY,
        "parent_registered": parent_registered,
        "scope_group": "not_requirement_effect_router",
        "preserved_sublanes": PRESERVED_SUBLANES,
        "policy_shape": "route_and_split",
        "decision_counts": dict(validation["decision_counts"]),
    }
    return {
        "agent_key": AGENT_KEY,
        "agent_type": "subcoordinator",
        "parent_agent_key": PARENT_AGENT_KEY,
        "status": "active",
        "operational_state": "shadow",
        "decision_role": "route_and_split",
        "scope_group": "not_requirement_effect_router",
        "scope_description": SCOPE_DESCRIPTION,
        "dashboard_group": "Issue Network",
        "priority": 42,
        "notes_json": notes,
    }


def dry_run_counts(validation: dict[str, Any]) -> dict[str, int]:
    return {
        "policies_to_register": 1,
        "review_total": int(validation["review_total"]),
        "dominant_sublane_count": int(validation["dominant_sublane_count"]),
        "registered_child_found": int(validation["registered_child_found"]),
        "requires_apply_later": int(validation["requires_apply_later"]),
        "requires_lifecycle_later": int(validation["requires_lifecycle_later"]),
        "auto_apply_allowed": 0,
        "production_release_allowed": 0,
        "lifecycle_allowed": 0,
    }


def assert_apply_guards(counts: dict[str, int]) -> None:
    expected = {
        "policies_to_register": 1,
        "review_total": 48,
        "dominant_sublane_count": 42,
        "registered_child_found": 1,
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
    child = conn.execute("SELECT status FROM ml_agent_registry WHERE agent_key = ?", (CHILD_AGENT_KEY,)).fetchone()
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
        "parent_registered": bool(notes.get("parent_registered")),
        "review_total": int(notes.get("review_total") or 0),
        "dominant_sublane_count": int(notes.get("dominant_sublane_count") or 0),
        "child_active": bool(child and child["status"] == "active"),
        "source": notes.get("source"),
    }


def write_reports(
    *,
    apply: bool,
    record: dict[str, Any],
    counts: dict[str, int],
    registry_validation: dict[str, Any] | None,
    created: int,
    updated: int,
    preexisting: bool,
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
            "preexisting_agent": preexisting,
            "parent_registered": record["notes_json"]["parent_registered"],
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
            "read_only_component": True,
            "terminal_policy": False,
            "requires_lifecycle_later": False,
            "requires_apply_later": False,
        }, ensure_ascii=False, sort_keys=True) + "\n")
    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(f"Not requirement/effect culture policy catalog registry {mode}\n\n")
        for key in [
            "policies_to_register",
            "review_total",
            "dominant_sublane_count",
            "registered_child_found",
            "requires_apply_later",
            "requires_lifecycle_later",
            "auto_apply_allowed",
            "production_release_allowed",
            "lifecycle_allowed",
        ]:
            handle.write(f"- {key}: {counts[key]}\n")
        handle.write(f"- parent_registered: {record['notes_json']['parent_registered']}\n")
        handle.write(f"- created: {created}\n")
        handle.write(f"- updated: {updated}\n")
        if registry_validation:
            handle.write(f"- registry_validation: {json.dumps(registry_validation, ensure_ascii=False, sort_keys=True)}\n")
        handle.write("\nAgent\n")
        handle.write(f"- {record['agent_key']} [{record['agent_type']}/{record['operational_state']}/{record['decision_role']}]\n")
    return txt_path, jsonl_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Register not_requirement_effect culture policy as read-only splitter.")
    parser.add_argument("--culture-jsonl", required=True, type=Path)
    parser.add_argument("--culture-spec-json", required=True, type=Path)
    parser.add_argument("--tradition-registry-jsonl", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    culture_rows = read_jsonl(args.culture_jsonl)
    culture_spec = read_json(args.culture_spec_json)
    tradition_rows = read_jsonl(args.tradition_registry_jsonl)
    culture_summary = summary_row(culture_rows, "culture review")
    tradition_apply_summary = summary_row(tradition_rows, "tradition registry")
    culture_samples = sample_rows(culture_rows)
    with connect_readonly() as ro_conn:
        registry_info = registry_preflight(ro_conn)
    validation = validate_inputs(
        culture_spec=culture_spec,
        culture_summary=culture_summary,
        culture_samples=culture_samples,
        tradition_apply_summary=tradition_apply_summary,
        registry_info=registry_info,
    )
    record = planned_record(validation, parent_registered=bool(registry_info["parent_registered"]))
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
        preexisting=bool(registry_info["preexisting"]),
    )
    print(f"txt: {txt_path}")
    print(f"jsonl: {jsonl_path}")
    print(f"mode: {'apply' if args.apply else 'dry_run'}")
    for key, value in counts.items():
        print(f"{key}: {value}")
    print(f"parent_registered: {record['notes_json']['parent_registered']}")
    print(f"created: {created}")
    print(f"updated: {updated}")
    print(f"preexisting_agent: {registry_info['preexisting']}")
    if registry_validation:
        print(f"registry_validation: {json.dumps(registry_validation, ensure_ascii=False, sort_keys=True)}")


if __name__ == "__main__":
    main()
