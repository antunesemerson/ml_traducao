from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "not_requirement_effect_culture_tradition_heritage_terminal_registry_v1"
AGENT_KEY = "not_requirement_effect_culture_tradition_heritage_policy"
PARENT_AGENT_KEY = "not_requirement_effect_culture_policy"
EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76
EXPECTED_REGISTERED_AGENTS_BEFORE = 225
EXPECTED_REVIEW_TOTAL = 42
EXPECTED_TERMINAL_MINIMUM = 32
EXPECTED_TERMINAL_DECISION = "tradition_heritage_terminal_policy_with_domain_guard"
EXPECTED_SOURCE_DECISION = "needs_culture_tradition_heritage_policy"
SCOPE_DESCRIPTION = (
    "Read-only terminal guard for not-requirement/effect culture tradition and "
    "heritage routes, with domain guard and preserved culture-title/residual leftovers."
)


def output_paths(apply: bool) -> tuple[Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    suffix = "apply" if apply else "dry_run"
    base = reports_dir / f"{stamp}_not_requirement_effect_culture_tradition_heritage_terminal_registry_{suffix}"
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
    exists = conn.execute("SELECT 1 FROM ml_agent_registry WHERE agent_key = ?", (AGENT_KEY,)).fetchone() is not None
    parent_exists = conn.execute("SELECT 1 FROM ml_agent_registry WHERE agent_key = ?", (PARENT_AGENT_KEY,)).fetchone() is not None
    return exists, parent_exists, registry_count


def validate_inputs(
    *,
    spec: dict[str, Any],
    summary: dict[str, Any],
    samples: list[dict[str, Any]],
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
    if len(samples) != EXPECTED_REVIEW_TOTAL:
        raise SystemExit(f"review_total guard failed: {len(samples)} expected {EXPECTED_REVIEW_TOTAL}")
    if int(summary.get("total_reviewed") or 0) != EXPECTED_REVIEW_TOTAL:
        raise SystemExit("summary total_reviewed guard failed")
    if int(summary.get("terminal_policy_count") or 0) < EXPECTED_TERMINAL_MINIMUM:
        raise SystemExit("summary terminal_policy_count guard failed")
    if int(summary.get("ready_lifecycle_future") or 0) != 0:
        raise SystemExit("summary ready_lifecycle_future guard failed")
    if int(summary.get("apply_candidates_future") or 0) != 0:
        raise SystemExit("summary apply_candidates_future guard failed")
    if summary.get("requires_apply_later") is not False:
        raise SystemExit("summary requires_apply_later guard failed")
    if summary.get("requires_lifecycle_later") is not False:
        raise SystemExit("summary requires_lifecycle_later guard failed")

    source_bad = [row["segment_id"] for row in samples if row.get("source_decision") != EXPECTED_SOURCE_DECISION]
    if source_bad:
        raise SystemExit(f"source_decision guard failed: {source_bad[:5]}")
    apply_count = sum(1 for row in samples if row.get("requires_apply_later") is True)
    lifecycle_count = sum(1 for row in samples if row.get("requires_lifecycle_later") is True)
    if apply_count:
        raise SystemExit(f"requires_apply_later guard failed: {apply_count}")
    if lifecycle_count:
        raise SystemExit(f"requires_lifecycle_later guard failed: {lifecycle_count}")
    decision_counts = Counter(str(row.get("tradition_heritage_decision") or "") for row in samples)
    terminal_count = int(decision_counts.get(EXPECTED_TERMINAL_DECISION, 0))
    if terminal_count < EXPECTED_TERMINAL_MINIMUM:
        raise SystemExit(f"{EXPECTED_TERMINAL_DECISION} guard failed: {terminal_count}")
    leftovers = {
        "needs_tradition_heritage_culture_title_policy": int(decision_counts.get("needs_tradition_heritage_culture_title_policy", 0)),
        "needs_tradition_heritage_residual_repair": int(decision_counts.get("needs_tradition_heritage_residual_repair", 0)),
    }
    return {
        "review_total": len(samples),
        "terminal_policies": terminal_count,
        "requires_apply_later": apply_count,
        "requires_lifecycle_later": lifecycle_count,
        "decision_counts": decision_counts,
        "leftovers": leftovers,
    }


def planned_record(validation: dict[str, Any], *, parent_registered: bool) -> dict[str, Any]:
    notes = {
        "source": SOURCE,
        "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
        "ledger_run_id": EXPECTED_LEDGER_RUN_ID,
        "review_total": int(validation["review_total"]),
        "terminal_policies": int(validation["terminal_policies"]),
        "tradition_heritage_terminal_policy_with_domain_guard": int(validation["terminal_policies"]),
        "requires_apply_later": 0,
        "requires_lifecycle_later": 0,
        "auto_apply_allowed": 0,
        "production_release_allowed": 0,
        "lifecycle_allowed": 0,
        "read_only_component": True,
        "terminal_policy": True,
        "terminal_guard": True,
        "parent_policy": PARENT_AGENT_KEY,
        "parent_registered": parent_registered,
        "primary_gap": "not_requirement_effect",
        "domain_guard": True,
        "policy_shape": "terminal_guard",
        "leftovers": validation["leftovers"],
        "decision_counts": dict(validation["decision_counts"]),
    }
    return {
        "agent_key": AGENT_KEY,
        "agent_type": "symbolic_subpolicy",
        "parent_agent_key": PARENT_AGENT_KEY,
        "status": "active",
        "operational_state": "dry_run",
        "decision_role": "terminal_guard",
        "scope_group": "not_requirement_effect_router",
        "scope_description": SCOPE_DESCRIPTION,
        "dashboard_group": "Issue Network",
        "priority": 43,
        "notes_json": notes,
    }


def dry_run_counts(validation: dict[str, Any]) -> dict[str, int]:
    return {
        "policies_to_register": 1,
        "review_total": int(validation["review_total"]),
        "terminal_policies": int(validation["terminal_policies"]),
        "tradition_heritage_terminal_policy_with_domain_guard": int(validation["terminal_policies"]),
        "requires_apply_later": int(validation["requires_apply_later"]),
        "requires_lifecycle_later": int(validation["requires_lifecycle_later"]),
        "auto_apply_allowed": 0,
        "production_release_allowed": 0,
        "lifecycle_allowed": 0,
    }


def assert_apply_guards(counts: dict[str, int]) -> None:
    expected = {
        "policies_to_register": 1,
        "review_total": 42,
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
    if counts.get("tradition_heritage_terminal_policy_with_domain_guard", 0) < EXPECTED_TERMINAL_MINIMUM:
        raise SystemExit("apply guard failed: tradition_heritage_terminal_policy_with_domain_guard")


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
        "parent_registered": bool(notes.get("parent_registered")),
        "review_total": int(notes.get("review_total") or 0),
        "terminal_policies": int(notes.get("terminal_policies") or 0),
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
            "terminal_policy": True,
            "read_only_component": True,
            "requires_lifecycle_later": False,
            "requires_apply_later": False,
        }, ensure_ascii=False, sort_keys=True) + "\n")
    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(f"Not requirement/effect culture tradition/heritage terminal registry {mode}\n\n")
        for key in [
            "policies_to_register",
            "review_total",
            "terminal_policies",
            "tradition_heritage_terminal_policy_with_domain_guard",
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
    parser = argparse.ArgumentParser(description="Register tradition/heritage terminal read-only policy.")
    parser.add_argument("--review-jsonl", required=True, type=Path)
    parser.add_argument("--spec-json", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    spec = read_json(args.spec_json)
    rows = read_jsonl(args.review_jsonl)
    summary = review_summary(rows)
    samples = sample_rows(rows)
    with connect_readonly() as ro_conn:
        preexisting, parent_registered, registry_count = registry_preflight(ro_conn)
    validation = validate_inputs(
        spec=spec,
        summary=summary,
        samples=samples,
        registry_count=registry_count,
    )
    record = planned_record(validation, parent_registered=parent_registered)
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
        preexisting=preexisting,
    )
    print(f"txt: {txt_path}")
    print(f"jsonl: {jsonl_path}")
    print(f"mode: {'apply' if args.apply else 'dry_run'}")
    for key, value in counts.items():
        print(f"{key}: {value}")
    print(f"parent_registered: {parent_registered}")
    print(f"created: {created}")
    print(f"updated: {updated}")
    print(f"preexisting_agent: {preexisting}")
    if registry_validation:
        print(f"registry_validation: {json.dumps(registry_validation, ensure_ascii=False, sort_keys=True)}")


if __name__ == "__main__":
    main()
