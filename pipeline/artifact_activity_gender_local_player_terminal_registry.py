from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "artifact_activity_gender_local_player_terminal_registry_v1"
AGENT_KEY = "artifact_activity_gender_local_player_policy"
PARENT_AGENT_KEY = "effect_list_artifact_activity_policy"
EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76
EXPECTED_REVIEW_TOTAL = 23
EXPECTED_REUSE_MINIMUM = 20
REUSED_POLICIES = [
    "local_player_requirement_policy",
    "select_cstring_es_helper_policy",
    "select_cstring_player_target_direct_policy",
    "select_cstring_possessive_policy",
    "es_oa_requirement_policy",
]
SCOPE_DESCRIPTION = (
    "Read-only terminal guard for artifact/activity effect-list cases that reuse registered "
    "gender/local-player and Select_CString policies with artifact/activity guards."
)


def output_paths(apply: bool) -> tuple[Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    suffix = "apply" if apply else "dry_run"
    base = reports_dir / f"{stamp}_artifact_activity_gender_local_player_terminal_registry_{suffix}"
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


def active_reused_policies(conn: sqlite3.Connection) -> set[str]:
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
    return {str(row["agent_key"]) for row in rows}


def validate_inputs(spec: dict[str, Any], summary: dict[str, Any], samples: list[dict[str, Any]], active_reused: set[str]) -> dict[str, Any]:
    if spec.get("policy_id") != AGENT_KEY:
        raise SystemExit(f"spec policy_id guard failed: {spec.get('policy_id')}")
    if spec.get("parent_policy") != PARENT_AGENT_KEY:
        raise SystemExit(f"spec parent_policy guard failed: {spec.get('parent_policy')}")
    if int(spec.get("segment_state_run_id") or 0) != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("spec segment_state_run_id guard failed")
    if int(spec.get("ledger_run_id") or 0) != EXPECTED_LEDGER_RUN_ID:
        raise SystemExit("spec ledger_run_id guard failed")

    review_total = len(samples)
    if review_total != EXPECTED_REVIEW_TOTAL:
        raise SystemExit(f"review_total guard failed: {review_total} expected {EXPECTED_REVIEW_TOTAL}")
    if int(summary.get("total_reviewed") or 0) != EXPECTED_REVIEW_TOTAL:
        raise SystemExit("summary total_reviewed guard failed")
    if int(summary.get("pending_count") or 0) != EXPECTED_REVIEW_TOTAL:
        raise SystemExit("summary pending_count guard failed")

    requires_apply = sum(1 for row in samples if row.get("requires_apply_later") is True)
    requires_lifecycle = sum(1 for row in samples if row.get("requires_lifecycle_later") is True)
    if requires_apply:
        raise SystemExit(f"requires_apply_later guard failed: {requires_apply}")
    if requires_lifecycle:
        raise SystemExit(f"requires_lifecycle_later guard failed: {requires_lifecycle}")

    reuse_count = sum(1 for row in samples if str(row.get("artifact_gender_decision") or "").startswith("artifact_gender_reuse_"))
    if reuse_count < EXPECTED_REUSE_MINIMUM:
        raise SystemExit(f"reuse_registered_policies guard failed: {reuse_count} expected >= {EXPECTED_REUSE_MINIMUM}")
    missing_reused = sorted(set(REUSED_POLICIES) - active_reused)
    if missing_reused:
        raise SystemExit(f"missing reused policies: {missing_reused}")

    return {
        "review_total": review_total,
        "reuse_registered_policies": reuse_count,
        "requires_apply_later": requires_apply,
        "requires_lifecycle_later": requires_lifecycle,
        "missing_reused_policy_keys": len(missing_reused),
        "decision_counts": Counter(str(row.get("artifact_gender_decision") or "") for row in samples),
        "matched_policy_counts": Counter(str(row.get("matched_registered_policy") or "") for row in samples if row.get("matched_registered_policy")),
    }


def planned_record(validation: dict[str, Any]) -> dict[str, Any]:
    notes = {
        "source": SOURCE,
        "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
        "ledger_run_id": EXPECTED_LEDGER_RUN_ID,
        "review_total": int(validation["review_total"]),
        "reuse_registered_policies": int(validation["reuse_registered_policies"]),
        "requires_apply_later": 0,
        "requires_lifecycle_later": 0,
        "auto_apply_allowed": 0,
        "production_release_allowed": 0,
        "lifecycle_allowed": 0,
        "read_only_component": True,
        "terminal_policy": True,
        "parent_policy": PARENT_AGENT_KEY,
        "reused_policies": REUSED_POLICIES,
        "decision_counts": dict(validation["decision_counts"]),
        "matched_policy_counts": dict(validation["matched_policy_counts"]),
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
        "priority": 31,
        "notes_json": notes,
    }


def dry_run_counts(validation: dict[str, Any], active_reused: set[str]) -> dict[str, int]:
    notes = planned_record(validation)["notes_json"]
    return {
        "policies_to_register": 1,
        "review_total": int(validation["review_total"]),
        "reuse_registered_policies": int(validation["reuse_registered_policies"]),
        "missing_reused_policy_keys": len(set(REUSED_POLICIES) - active_reused),
        "requires_apply_later": int(validation["requires_apply_later"]),
        "requires_lifecycle_later": int(validation["requires_lifecycle_later"]),
        "auto_apply_allowed": int(notes["auto_apply_allowed"]),
        "production_release_allowed": int(notes["production_release_allowed"]),
        "lifecycle_allowed": int(notes["lifecycle_allowed"]),
    }


def assert_apply_guards(counts: dict[str, int]) -> None:
    expected_exact = {
        "policies_to_register": 1,
        "review_total": 23,
        "missing_reused_policy_keys": 0,
        "requires_apply_later": 0,
        "requires_lifecycle_later": 0,
        "auto_apply_allowed": 0,
        "production_release_allowed": 0,
        "lifecycle_allowed": 0,
    }
    for key, value in expected_exact.items():
        if counts.get(key) != value:
            raise SystemExit(f"apply guard failed: {key}={counts.get(key)} expected {value}")
    if counts.get("reuse_registered_policies", 0) < EXPECTED_REUSE_MINIMUM:
        raise SystemExit("apply guard failed: reuse_registered_policies")


def upsert_record(conn: sqlite3.Connection, record: dict[str, Any]) -> tuple[int, int]:
    now = db.utc_now()
    exists = conn.execute(
        "SELECT 1 FROM ml_agent_registry WHERE agent_key = ?",
        (record["agent_key"],),
    ).fetchone()
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
    row = conn.execute(
        """
        SELECT *
        FROM ml_agent_registry
        WHERE agent_key = ?
        """,
        (AGENT_KEY,),
    ).fetchone()
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
        "auto_apply_allowed": int(notes.get("auto_apply_allowed") or 0),
        "production_release_allowed": int(notes.get("production_release_allowed") or 0),
        "lifecycle_allowed": int(notes.get("lifecycle_allowed") or 0),
        "source": notes.get("source"),
        "review_total": int(notes.get("review_total") or 0),
        "reuse_registered_policies": int(notes.get("reuse_registered_policies") or 0),
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
                    "read_only_component": record["notes_json"].get("read_only_component", False),
                    "reused_policies": record["notes_json"].get("reused_policies", []),
                    "requires_lifecycle_later": False,
                    "requires_apply_later": False,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )
    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(f"Artifact/activity gender-local-player terminal registry {mode}\n\n")
        for key in [
            "policies_to_register",
            "review_total",
            "reuse_registered_policies",
            "missing_reused_policy_keys",
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
        handle.write(
            f"- {record['agent_key']} [{record['agent_type']}/{record['operational_state']}/{record['decision_role']}]\n"
        )
    return txt_path, jsonl_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Register artifact/activity gender-local-player terminal read-only policy.")
    parser.add_argument("--review-jsonl", required=True, type=Path)
    parser.add_argument("--spec-json", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    spec = read_json(args.spec_json)
    rows = read_jsonl(args.review_jsonl)
    summary = review_summary(rows)
    samples = sample_rows(rows)
    with connect_readonly() as ro_conn:
        active_reused = active_reused_policies(ro_conn)
    validation = validate_inputs(spec, summary, samples, active_reused)
    record = planned_record(validation)
    counts = dry_run_counts(validation, active_reused)
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
    if registry_validation:
        print(f"registry_validation: {json.dumps(registry_validation, ensure_ascii=False, sort_keys=True)}")


if __name__ == "__main__":
    main()
