from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "holy_site_effect_name_singular_token_allowlist_policy_v1"
AGENT_KEY = "holy_site_effect_name_singular_token_allowlist_policy"
SEGMENT_IDS = [237388, 239477, 239479, 239507, 239509, 239511]
REVIEW_SUMMARY = Path(
    "reports/20260701_010435_612969_domain_policy_vote_candidate_holy_site_token_changing_review_summary.json"
)
REVIEW_JSONL = Path(
    "reports/20260701_010435_612969_domain_policy_vote_candidate_holy_site_token_changing_review.jsonl"
)


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json(path: Path) -> dict[str, Any]:
    with db.project_path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with db.project_path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def validate_review(summary: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    if int(summary.get("record_count") or 0) != 6:
        raise SystemExit("review record_count guard failed")
    if not bool(summary.get("all_six_safe_for_readonly_allowlist_policy")):
        raise SystemExit("review safety guard failed")
    if int(summary.get("candidate_generation_count") or 0) != 0:
        raise SystemExit("review candidate_generation guard failed")
    if int(summary.get("apply_count") or 0) != 0:
        raise SystemExit("review apply guard failed")
    if int(summary.get("lifecycle_count") or 0) != 0:
        raise SystemExit("review lifecycle guard failed")
    found = {int(row.get("segment_id") or 0) for row in rows}
    if found != set(SEGMENT_IDS):
        raise SystemExit(f"review segment guard failed: {sorted(found)}")
    unsafe = [row for row in rows if row.get("review_decision") != "safe_for_readonly_allowlist_policy"]
    if unsafe:
        raise SystemExit("review decision guard failed")


def registry_record(summary: dict[str, Any]) -> dict[str, Any]:
    notes = {
        "source": SOURCE,
        "review_summary": str(REVIEW_SUMMARY),
        "review_jsonl": str(REVIEW_JSONL),
        "source_family": "holy_site_token_changing_hold",
        "allowed_exact_change": {"from": "[holy_sites|lE]", "to": "[holy_site|lE]"},
        "required_source_key_regex": r"^holy_site_[a-z0-9_]+_effect_name$",
        "required_text_shape": "same prefix, same #weak ($holy_site_*_name$)#! wrapper, same variable",
        "focus_segment_ids": SEGMENT_IDS,
        "review_record_count": int(summary.get("record_count") or 0),
        "candidate_generation_allowed": False,
        "auto_apply_allowed": False,
        "lifecycle_allowed": False,
        "production_release_allowed": False,
        "requires_protected_apply_later": True,
        "read_only_component": True,
    }
    return {
        "agent_key": AGENT_KEY,
        "agent_type": "symbolic_subpolicy",
        "parent_agent_key": "domain_policy_vote_candidate",
        "status": "active",
        "operational_state": "shadow",
        "decision_role": "token_policy_allowlist",
        "scope_group": "domain_policy_vote_candidate",
        "scope_description": "Read-only allowlist for exact holy_site effect_name singular token correction.",
        "dashboard_group": "Issue Network",
        "priority": 34,
        "notes_json": notes,
    }


def upsert_record(conn: sqlite3.Connection, record: dict[str, Any]) -> tuple[int, int]:
    now = db.utc_now()
    exists = conn.execute("SELECT 1 FROM ml_agent_registry WHERE agent_key = ?", (record["agent_key"],)).fetchone()
    created = 0 if exists else 1
    updated = 1 if exists else 0
    conn.execute(
        """
        INSERT INTO ml_agent_registry (
            agent_key, agent_type, parent_agent_key, model_kind, status,
            operational_state, decision_role, scope_group, scope_sql,
            scope_description, default_threshold, priority, dashboard_group,
            created_at, updated_at, notes_json
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
        "agent_key": payload.get("agent_key"),
        "agent_type": payload.get("agent_type"),
        "parent_agent_key": payload.get("parent_agent_key"),
        "status": payload.get("status"),
        "operational_state": payload.get("operational_state"),
        "decision_role": payload.get("decision_role"),
        "scope_group": payload.get("scope_group"),
        "dashboard_group": payload.get("dashboard_group"),
        "candidate_generation_allowed": bool(notes.get("candidate_generation_allowed")),
        "auto_apply_allowed": bool(notes.get("auto_apply_allowed")),
        "lifecycle_allowed": bool(notes.get("lifecycle_allowed")),
        "production_release_allowed": bool(notes.get("production_release_allowed")),
        "focus_segment_count": len(notes.get("focus_segment_ids") or []),
    }


def write_reports(
    *,
    mode: str,
    record: dict[str, Any],
    created: int,
    updated: int,
    validation: dict[str, Any],
) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_{AGENT_KEY}_{mode}"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": mode,
        "agent_key": AGENT_KEY,
        "policies_to_register": 1,
        "created": created,
        "updated": updated,
        "registry_validation": validation,
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
    }
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps({"record_type": "summary", **summary}, ensure_ascii=False, sort_keys=True) + "\n")
        handle.write(json.dumps({"record_type": "registry_record", **record}, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    txt_path.write_text(
        "\n".join(
            [
                f"{AGENT_KEY} {mode}",
                f"created={created}",
                f"updated={updated}",
                "candidate_generation_count=0",
                "apply_count=0",
                "lifecycle_count=0",
                "segment_state_count=0",
                "reindex_count=0",
                "production_full_count=0",
                f"registry_validation={json.dumps(validation, ensure_ascii=False, sort_keys=True)}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return txt_path, jsonl_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    mode = "apply" if args.apply else "dry_run"
    summary = read_json(REVIEW_SUMMARY)
    rows = read_jsonl(REVIEW_JSONL)
    validate_review(summary, rows)
    record = registry_record(summary)
    created = updated = 0
    with connect_readonly() as conn:
        validation = validate_registry(conn)
    if args.apply:
        with db.connect(db.load_settings()) as conn:
            created, updated = upsert_record(conn, record)
            conn.commit()
            validation = validate_registry(conn)
        if not validation.get("exists"):
            raise SystemExit("registry validation failed after apply")
        if any(
            validation.get(key)
            for key in [
                "candidate_generation_allowed",
                "auto_apply_allowed",
                "lifecycle_allowed",
                "production_release_allowed",
            ]
        ):
            raise SystemExit("registry release guard failed")
    txt_path, jsonl_path, summary_path = write_reports(
        mode=mode,
        record=record,
        created=created,
        updated=updated,
        validation=validation,
    )
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"mode={mode}")
    print(f"created={created}")
    print(f"updated={updated}")
    print(f"registry_exists={validation.get('exists')}")


if __name__ == "__main__":
    main()
