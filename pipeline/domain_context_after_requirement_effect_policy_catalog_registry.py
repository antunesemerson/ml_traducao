from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_context_after_requirement_effect_policy_catalog_registry_v1"
AGENT_KEY = "domain_context_after_requirement_effect"
PARENT_AGENT_KEY = "requirement_effect_router_readonly"
EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76
EXPECTED_REGISTERED_AGENTS_BEFORE = 231
EXPECTED_REVIEW_TOTAL = 174
EXPECTED_REUSE_MINIMUM = 59
EXPECTED_UNIVERSE = 174
CHILD_POLICIES = [
    "domain_context_landed_title_dynasty_house_name_policy",
    "domain_context_religion_holy_site_policy",
]
REUSED_POLICIES = [
    "residual_repair_after_requirement_effect",
    "effect_list_gender_local_player_policy",
    "not_requirement_effect_culture_policy",
]
EXPECTED_DECISIONS = {
    "needs_domain_title_law_policy": 69,
    "needs_domain_religion_holy_site_policy": 44,
    "domain_context_reuse_requirement_effect_residual_policy": 41,
    "domain_context_reuse_effect_list_gender_local_player_policy": 11,
    "domain_context_reuse_not_requirement_effect_culture_policy": 7,
    "needs_domain_culture_name_policy": 2,
}
SCOPE_DESCRIPTION = (
    "Read-only domain-context splitter after requirement/effect; routes title/law, "
    "religion/holy-site, residual, gender/local-player and culture/name cases while "
    "preserving no apply/lifecycle permission."
)


def output_paths(apply: bool) -> tuple[Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    suffix = "apply" if apply else "dry_run"
    base = reports_dir / f"{stamp}_domain_context_after_requirement_effect_policy_catalog_registry_{suffix}"
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
    rows = conn.execute("SELECT agent_key, operational_state FROM ml_agent_registry").fetchall()
    keys = {str(row["agent_key"]) for row in rows}
    registered_agents = len(rows)
    if registered_agents not in {EXPECTED_REGISTERED_AGENTS_BEFORE, EXPECTED_REGISTERED_AGENTS_BEFORE + 1}:
        raise SystemExit(f"registry count guard failed: {registered_agents} expected {EXPECTED_REGISTERED_AGENTS_BEFORE}")
    child_found = sorted(key for key in CHILD_POLICIES if key in keys)
    missing_children = sorted(set(CHILD_POLICIES) - keys)
    reused_found = sorted(key for key in REUSED_POLICIES if key in keys)
    missing_reused = sorted(set(REUSED_POLICIES) - keys)
    return {
        "registered_agents": registered_agents,
        "preexisting_agent": AGENT_KEY in keys,
        "parent_registered": PARENT_AGENT_KEY in keys,
        "registered_child_found": len(child_found),
        "child_found": child_found,
        "missing_child_policy_keys": missing_children,
        "reused_found": reused_found,
        "missing_reused_policy_keys": missing_reused,
        "dry_run_agents": sum(1 for row in rows if row["operational_state"] == "dry_run"),
        "shadow_agents": sum(1 for row in rows if row["operational_state"] == "shadow"),
        "operational_agents": sum(1 for row in rows if row["operational_state"] == "operational"),
    }


def validate_inputs(
    *,
    spec: dict[str, Any],
    summary: dict[str, Any],
    samples: list[dict[str, Any]],
    preflight: dict[str, Any],
) -> dict[str, Any]:
    if spec.get("policy_id") != AGENT_KEY:
        raise SystemExit(f"spec policy_id guard failed: {spec.get('policy_id')}")
    if int(spec.get("segment_state_run_id") or 0) != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("spec segment_state_run_id guard failed")
    if int(spec.get("ledger_run_id") or 0) != EXPECTED_LEDGER_RUN_ID:
        raise SystemExit("spec ledger_run_id guard failed")
    if len(samples) != EXPECTED_REVIEW_TOTAL:
        raise SystemExit(f"review_total guard failed: {len(samples)} expected {EXPECTED_REVIEW_TOTAL}")
    if int(summary.get("total_reviewed") or 0) != EXPECTED_REVIEW_TOTAL:
        raise SystemExit("summary total_reviewed guard failed")
    if int(summary.get("universe_estimated") or 0) != EXPECTED_UNIVERSE:
        raise SystemExit("summary universe_estimated guard failed")
    if int(summary.get("reuse_registered_or_cataloged_count") or 0) < EXPECTED_REUSE_MINIMUM:
        raise SystemExit("summary reuse guard failed")
    if int(summary.get("terminal_policy_count") or 0) != 0:
        raise SystemExit("summary terminal_policy_count guard failed")
    if int(summary.get("ready_lifecycle_future") or 0) != 0:
        raise SystemExit("summary ready_lifecycle_future guard failed")
    if int(summary.get("apply_candidates_future") or 0) != 0:
        raise SystemExit("summary apply_candidates_future guard failed")
    if summary.get("requires_apply_later") is not False:
        raise SystemExit("summary requires_apply_later guard failed")
    if summary.get("requires_lifecycle_later") is not False:
        raise SystemExit("summary requires_lifecycle_later guard failed")
    if preflight["missing_child_policy_keys"]:
        raise SystemExit(f"missing child policies: {preflight['missing_child_policy_keys']}")

    apply_count = sum(1 for row in samples if row.get("requires_apply_later") is True)
    lifecycle_count = sum(1 for row in samples if row.get("requires_lifecycle_later") is True)
    if apply_count:
        raise SystemExit(f"requires_apply_later guard failed: {apply_count}")
    if lifecycle_count:
        raise SystemExit(f"requires_lifecycle_later guard failed: {lifecycle_count}")
    bad_route = [row["segment_id"] for row in samples if row.get("primary_route") != AGENT_KEY]
    if bad_route:
        raise SystemExit(f"primary_route guard failed: {bad_route[:5]}")
    decision_counts = Counter(str(row.get("domain_context_decision") or "") for row in samples)
    for decision, expected in EXPECTED_DECISIONS.items():
        if int(decision_counts.get(decision, 0)) != expected:
            raise SystemExit(f"decision guard failed: {decision}={decision_counts.get(decision, 0)} expected {expected}")
    return {
        "review_total": len(samples),
        "universe_estimated": int(summary.get("universe_estimated") or len(samples)),
        "reuse_cataloged_policies": int(summary.get("reuse_registered_or_cataloged_count") or 0),
        "terminal_local": int(summary.get("terminal_policy_count") or 0),
        "requires_apply_later": apply_count,
        "requires_lifecycle_later": lifecycle_count,
        "decision_counts": decision_counts,
    }


def planned_record(validation: dict[str, Any], *, parent_registered: bool, preflight: dict[str, Any]) -> dict[str, Any]:
    notes = {
        "source": SOURCE,
        "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
        "ledger_run_id": EXPECTED_LEDGER_RUN_ID,
        "universe_estimated": int(validation["universe_estimated"]),
        "review_total": int(validation["review_total"]),
        "reuse_cataloged_policies": int(validation["reuse_cataloged_policies"]),
        "terminal_local": int(validation["terminal_local"]),
        "policy_shape": "reuse_splitter_route",
        "requires_apply_later": 0,
        "requires_lifecycle_later": 0,
        "auto_apply_allowed": 0,
        "production_release_allowed": 0,
        "lifecycle_allowed": 0,
        "read_only_component": True,
        "terminal_policy": False,
        "parent_policy": PARENT_AGENT_KEY,
        "parent_registered": parent_registered,
        "primary_route": AGENT_KEY,
        "registered_child_policies": CHILD_POLICIES,
        "registered_child_found": int(preflight["registered_child_found"]),
        "missing_child_policy_keys": preflight["missing_child_policy_keys"],
        "reused_policies": REUSED_POLICIES,
        "missing_reused_policy_keys": preflight["missing_reused_policy_keys"],
        "dominant_sublanes": EXPECTED_DECISIONS,
        "matured_terminal_leaf": {
            "domain_context_landed_title_dynasty_house_name_policy": 61,
        },
        "matured_reuse_splitter": {
            "domain_context_religion_holy_site_policy": 44,
        },
        "preserved_sublanes": {
            "needs_domain_culture_name_policy": 2,
        },
        "decision_counts": dict(validation["decision_counts"]),
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
        "priority": 46,
        "notes_json": notes,
    }


def dry_run_counts(validation: dict[str, Any], preflight: dict[str, Any]) -> dict[str, int]:
    return {
        "policies_to_register": 1,
        "review_total": int(validation["review_total"]),
        "reuse_cataloged_policies": int(validation["reuse_cataloged_policies"]),
        "registered_child_found": int(preflight["registered_child_found"]),
        "missing_child_policy_keys": len(preflight["missing_child_policy_keys"]),
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
        "registered_child_found": 2,
        "missing_child_policy_keys": 0,
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
    children = set(notes.get("registered_child_policies") or [])
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
        "registered_child_policies_ok": set(CHILD_POLICIES).issubset(children),
        "review_total": int(notes.get("review_total") or 0),
        "reuse_cataloged_policies": int(notes.get("reuse_cataloged_policies") or 0),
        "source": notes.get("source"),
        "parent_registered": bool(notes.get("parent_registered")),
    }
    expected = {
        "exists": True,
        "status": "active",
        "operational_state": "shadow",
        "agent_type": "subcoordinator",
        "decision_role": "route_and_split",
        "parent_agent_key": PARENT_AGENT_KEY,
        "scope_group": "requirement_effect_router",
        "dashboard_group": "Issue Network",
        "auto_apply_allowed": 0,
        "production_release_allowed": 0,
        "lifecycle_allowed": 0,
        "registered_child_policies_ok": True,
        "review_total": EXPECTED_REVIEW_TOTAL,
        "source": SOURCE,
    }
    for key, value in expected.items():
        if validation.get(key) != value:
            raise SystemExit(f"registry validation failed: {key}={validation.get(key)} expected {value}")
    if validation["reuse_cataloged_policies"] < EXPECTED_REUSE_MINIMUM:
        raise SystemExit("registry validation failed: reuse_cataloged_policies")
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
            "registered_child_policies": CHILD_POLICIES,
            "requires_lifecycle_later": False,
            "requires_apply_later": False,
        }, ensure_ascii=False, sort_keys=True) + "\n")
    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(f"Domain context after requirement/effect policy catalog registry {mode}\n\n")
        for key in [
            "policies_to_register",
            "review_total",
            "reuse_cataloged_policies",
            "registered_child_found",
            "missing_child_policy_keys",
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
    parser = argparse.ArgumentParser(description="Register domain-context after requirement/effect read-only splitter.")
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
    validation = validate_inputs(spec=spec, summary=summary, samples=samples, preflight=preflight)
    record = planned_record(validation, parent_registered=bool(preflight["parent_registered"]), preflight=preflight)
    counts = dry_run_counts(validation, preflight)
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
    print(f"parent_registered: {record['notes_json']['parent_registered']}")
    print(f"created: {created}")
    print(f"updated: {updated}")
    print(f"preexisting_agent: {preflight['preexisting_agent']}")
    if registry_validation:
        print(f"registry_validation: {json.dumps(registry_validation, ensure_ascii=False, sort_keys=True)}")


if __name__ == "__main__":
    main()
