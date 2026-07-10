from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_religion_name_route_subpolicy_dry_run_v1"
AGENT_KEY = "domain_policy_vote_candidate_religion_name_route_subpolicy"
INPUT_PATH = Path("reports/20260630_083757_445077_domain_policy_vote_candidate_religion_faith_getter_religion_name_route_review.jsonl")
EXPECTED_TOTAL = 63
ROUTE_SUBTYPES = {
    "faith_conversion_text": "route_religion_name_faith_conversion_text",
    "religion_family_reference": "route_religion_name_family_reference",
    "generic_religion_name_reference": "route_religion_name_generic_reference",
    "ui_tooltip_religion_reference": "route_religion_name_ui_tooltip_reference",
}
HOLD_SUBTYPES = {
    "placeholder_debug_hold": "hold_religion_name_placeholder_debug",
    "holy_site_effect_or_requirement": "hold_religion_name_holy_site_effect",
    "holy_war_fervor_context": "hold_religion_name_holy_war_fervor",
    "religion_family_requirement": "hold_religion_name_family_requirement",
}


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def fetch_registry(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM ml_agent_registry WHERE agent_key = ?", (AGENT_KEY,)).fetchone()
    if row is None:
        raise SystemExit(f"missing registry agent: {AGENT_KEY}")
    payload = dict(row)
    notes = json.loads(payload.get("notes_json") or "{}")
    return {
        "agent_key": payload.get("agent_key"),
        "agent_type": payload.get("agent_type"),
        "status": payload.get("status"),
        "operational_state": payload.get("operational_state"),
        "decision_role": payload.get("decision_role"),
        "parent_agent_key": payload.get("parent_agent_key"),
        "scope_group": payload.get("scope_group"),
        "dashboard_group": payload.get("dashboard_group"),
        "candidate_generation_allowed": bool(notes.get("candidate_generation_allowed")),
        "auto_apply_allowed": bool(notes.get("auto_apply_allowed")),
        "lifecycle_allowed": bool(notes.get("lifecycle_allowed")),
        "production_release_allowed": bool(notes.get("production_release_allowed")),
        "route_subtypes": notes.get("route_subtypes") or {},
        "hold_subtypes": notes.get("hold_subtypes") or {},
        "route_count": int(notes.get("route_count") or 0),
        "hold_count": int(notes.get("hold_count") or 0),
    }


def validate_registry(registry: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    expected = {
        "agent_type": "subcoordinator",
        "status": "active",
        "operational_state": "shadow",
        "decision_role": "route_and_split",
        "parent_agent_key": "domain_policy_vote_candidate_religion_faith_getter_role_policy",
        "scope_group": "domain_policy_vote_candidate",
        "dashboard_group": "Issue Network",
        "route_count": 9,
        "hold_count": 54,
    }
    for key, value in expected.items():
        if registry.get(key) != value:
            reasons.append(f"registry_{key}_mismatch")
    for key in ("candidate_generation_allowed", "auto_apply_allowed", "lifecycle_allowed", "production_release_allowed"):
        if registry.get(key) is not False:
            reasons.append(f"registry_{key}_not_false")
    if registry.get("route_subtypes") != ROUTE_SUBTYPES:
        reasons.append("registry_route_subtypes_mismatch")
    if registry.get("hold_subtypes") != HOLD_SUBTYPES:
        reasons.append("registry_hold_subtypes_mismatch")
    return reasons


def route_row(row: dict[str, Any]) -> dict[str, Any]:
    subtype = str(row.get("operational_subtype") or "")
    if subtype in ROUTE_SUBTYPES:
        route = ROUTE_SUBTYPES[subtype]
        route_status = "split_only_no_candidate"
        hold_reason = None
    else:
        route = HOLD_SUBTYPES.get(subtype, f"hold_{subtype or 'unknown'}")
        route_status = "hold_context"
        hold_reason = row.get("subtype_recommendation") or row.get("review_action")
    return {
        **row,
        "agent_key": AGENT_KEY,
        "subpolicy_route": route,
        "subpolicy_route_status": route_status,
        "subpolicy_hold_reason": hold_reason,
        "candidate_generation_allowed": False,
        "auto_apply_allowed": False,
        "lifecycle_allowed": False,
        "production_release_allowed": False,
    }


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def top_counter(counter: Counter[str]) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in counter.most_common()]


def write_txt(path: Path, summary: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    lines = [
        "domain_policy_vote_candidate religion_name route subpolicy dry-run",
        "",
        f"agent_key: {AGENT_KEY}",
        f"review_count: {summary['review_count']}",
        f"split_only_no_candidate_count: {summary['split_only_no_candidate_count']}",
        f"hold_context_count: {summary['hold_context_count']}",
        f"registry_guard_ok: {str(summary['registry_guard_ok']).lower()}",
        "",
        "route_counts:",
    ]
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["route_counts"])
    lines.extend(["", "subtype_counts:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["subtype_counts"])
    lines.extend(["", "items:"])
    for row in rows:
        lines.extend(
            [
                "",
                f"## segment_id {row['segment_id']} | {row['operational_subtype']} -> {row['subpolicy_route']}",
                f"- route_status: {row['subpolicy_route_status']}",
                f"- source_key: {row.get('source_key')}",
                f"- output: {row.get('current_output_text')}",
            ]
        )
    lines.extend(["", "gates:", "- candidate_generation: not_run", "- apply: not_run", "- lifecycle: not_run", "- segment_state: not_run", "- reindex: not_run", "- full_production: not_run"])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    rows = read_jsonl(INPUT_PATH)
    if len(rows) != EXPECTED_TOTAL:
        raise SystemExit(f"input count guard failed: {len(rows)} expected {EXPECTED_TOTAL}")
    segment_ids = [int(row["segment_id"]) for row in rows]
    if len(segment_ids) != len(set(segment_ids)):
        raise SystemExit("duplicate segment_id guard failed")

    with connect_readonly() as conn:
        registry = fetch_registry(conn)
    registry_reasons = validate_registry(registry)

    routed = [route_row(row) for row in rows]
    routed.sort(key=lambda row: (str(row["subpolicy_route_status"]), str(row["subpolicy_route"]), int(row["segment_id"])))
    route_counts = Counter(row["subpolicy_route"] for row in routed)
    status_counts = Counter(row["subpolicy_route_status"] for row in routed)
    subtype_counts = Counter(row["operational_subtype"] for row in routed)
    if status_counts.get("split_only_no_candidate", 0) != 9 or status_counts.get("hold_context", 0) != 54:
        raise SystemExit("split/hold status guard failed")

    summary: dict[str, Any] = {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "read_only_subpolicy_dry_run",
        "agent_key": AGENT_KEY,
        "input_path": str(INPUT_PATH),
        "review_count": len(routed),
        "expected_count": EXPECTED_TOTAL,
        "count_matches_expected": len(routed) == EXPECTED_TOTAL,
        "registry": registry,
        "registry_guard_ok": not registry_reasons,
        "registry_guard_reasons": registry_reasons,
        "route_counts": top_counter(route_counts),
        "route_status_counts": top_counter(status_counts),
        "subtype_counts": top_counter(subtype_counts),
        "split_only_no_candidate_count": status_counts.get("split_only_no_candidate", 0),
        "hold_context_count": status_counts.get("hold_context", 0),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "next_recommendation": "Review the 9 split_only_no_candidate rows together as a tiny human/policy packet; keep the 54 holds out of candidate generation.",
        "gates": {
            "candidate_generation": "not_run",
            "apply": "not_run",
            "lifecycle": "not_run",
            "segment_state": "not_run",
            "reindex": "not_run",
            "full_production": "not_run",
        },
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "output_files": {},
    }
    base = reports_dir() / f"{stamp()}_{AGENT_KEY}_dry_run"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    write_jsonl(jsonl_path, routed)
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_txt(txt_path, summary, routed)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
