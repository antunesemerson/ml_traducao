from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_religion_faith_getter_role_policy_dry_run_v1"
AGENT_KEY = "domain_policy_vote_candidate_religion_faith_getter_role_policy"
REVIEW_PATH = Path("reports/20260630_082516_069795_domain_policy_vote_candidate_religion_faith_getter_role_review.jsonl")
EXPECTED_TOTAL = 196
EXPECTED_SAFE_ROUTE_COUNT = 71
ROUTE_BY_ROLE = {
    "faith_name": "route_faith_getter_faith_name",
    "religion_name": "route_faith_getter_religion_name",
    "religion_family_name": "route_faith_getter_religion_family_name",
    "faith_adjective": "route_faith_getter_faith_adjective",
}
HOLD_BY_ROLE = {
    "faith_possessive": "hold_faith_getter_possessive",
    "dense_structural_getter_cluster": "hold_dense_structural_getter_cluster",
    "article_preposition_context": "hold_article_preposition_context",
    "unknown_hold": "hold_unknown_needs_context",
    "faith_high_god_or_divine_name": "hold_divine_runtime_name",
    "faith_adherent_getter": "hold_clergy_adherent_context",
    "faith_priest_or_clergy_getter": "hold_clergy_adherent_context",
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
        "route_roles": notes.get("route_roles") or {},
        "hold_roles": notes.get("hold_roles") or [],
        "safe_route_count": int(notes.get("safe_route_count") or 0),
    }


def validate_registry(registry: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    expected = {
        "agent_type": "subcoordinator",
        "status": "active",
        "operational_state": "shadow",
        "decision_role": "route_and_split",
        "parent_agent_key": "domain_policy_vote_candidate",
        "scope_group": "domain_policy_vote_candidate",
        "dashboard_group": "Issue Network",
        "safe_route_count": EXPECTED_SAFE_ROUTE_COUNT,
    }
    for key, value in expected.items():
        if registry.get(key) != value:
            reasons.append(f"registry_{key}_mismatch")
    for key in ("candidate_generation_allowed", "auto_apply_allowed", "lifecycle_allowed", "production_release_allowed"):
        if registry.get(key) is not False:
            reasons.append(f"registry_{key}_not_false")
    if registry.get("route_roles") != ROUTE_BY_ROLE:
        reasons.append("registry_route_roles_mismatch")
    if set(registry.get("hold_roles") or []) != set(HOLD_BY_ROLE):
        reasons.append("registry_hold_roles_mismatch")
    return reasons


def route_row(row: dict[str, Any]) -> dict[str, Any]:
    role = str(row.get("getter_role") or "")
    recommendation = str(row.get("role_recommendation") or "")
    safe_split = role in ROUTE_BY_ROLE and recommendation == "read_only_splitter_candidate"
    if safe_split:
        route = ROUTE_BY_ROLE[role]
        route_status = "split_only_no_candidate"
        hold_reason = None
    else:
        route = HOLD_BY_ROLE.get(role, f"hold_{role or 'unknown'}")
        route_status = "hold_context"
        hold_reason = recommendation or "not_marked_safe_for_splitter"
    return {
        "segment_id": int(row["segment_id"]),
        "agent_key": AGENT_KEY,
        "architecture_family": row.get("architecture_family"),
        "architecture_family_tags": row.get("architecture_family_tags"),
        "source_key": row.get("source_key"),
        "relative_path": row.get("relative_path"),
        "surface_bucket": row.get("surface_bucket"),
        "risk_bucket": row.get("risk_bucket"),
        "getter_role": role,
        "getter_role_tags": row.get("getter_role_tags"),
        "role_recommendation": recommendation,
        "route": route,
        "route_status": route_status,
        "hold_reason": hold_reason,
        "candidate_generation_allowed": False,
        "requires_human_context": route_status == "hold_context",
        "english_text": row.get("english_text"),
        "spanish_text": row.get("spanish_text"),
        "current_output_text": row.get("current_output_text"),
    }


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def top_counter(counter: Counter[str]) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in counter.most_common()]


def representative_examples(rows: list[dict[str, Any]], route: str, limit: int = 8) -> list[dict[str, Any]]:
    route_rows = [row for row in rows if row["route"] == route]
    route_rows.sort(key=lambda row: (str(row.get("risk_bucket") or ""), int(row["segment_id"])))
    return [
        {
            "segment_id": row["segment_id"],
            "risk_bucket": row.get("risk_bucket"),
            "getter_role": row.get("getter_role"),
            "route_status": row.get("route_status"),
            "source_key": row.get("source_key"),
            "current_output_text": row.get("current_output_text"),
            "english_text": row.get("english_text"),
        }
        for row in route_rows[:limit]
    ]


def write_txt(path: Path, summary: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    lines = [
        "domain_policy_vote_candidate religion faith getter role policy dry-run",
        "",
        f"agent_key: {AGENT_KEY}",
        f"review_count: {summary['review_count']}",
        f"safe_split_count: {summary['safe_split_count']}",
        f"hold_context_count: {summary['hold_context_count']}",
        f"registry_guard_ok: {str(summary['registry_guard_ok']).lower()}",
        "",
        "route_counts:",
    ]
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["route_counts"])
    lines.extend(["", "route_status_counts:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["route_status_counts"])
    lines.extend(["", "role_x_route_counts:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["role_x_route_counts"])
    lines.extend(["", "safe_route_representative_examples:"])
    for route, examples in summary["safe_route_representative_examples"].items():
        lines.extend(["", f"## {route}"])
        for row in examples:
            lines.extend(
                [
                    f"- segment_id {row['segment_id']} | {row['risk_bucket']} | {row['source_key']}",
                    f"  output: {row['current_output_text']}",
                ]
            )
    lines.extend(
        [
            "",
            "next_recommendation:",
            f"- {summary['next_recommendation']}",
            "",
            "gates:",
            "- candidate_generation: not_run",
            "- apply: not_run",
            "- lifecycle: not_run",
            "- segment_state: not_run",
            "- reindex: not_run",
            "- full_production: not_run",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    review_rows = read_jsonl(REVIEW_PATH)
    if len(review_rows) != EXPECTED_TOTAL:
        raise SystemExit(f"review count guard failed: {len(review_rows)} expected {EXPECTED_TOTAL}")
    segment_ids = [int(row["segment_id"]) for row in review_rows]
    if len(segment_ids) != len(set(segment_ids)):
        raise SystemExit("duplicate segment_id guard failed")

    with connect_readonly() as conn:
        registry = fetch_registry(conn)
    registry_reasons = validate_registry(registry)

    routed = [route_row(row) for row in review_rows]
    routed.sort(key=lambda row: (str(row["route_status"]), str(row["route"]), str(row.get("risk_bucket") or ""), int(row["segment_id"])))

    route_counts = Counter(row["route"] for row in routed)
    route_status_counts = Counter(row["route_status"] for row in routed)
    role_counts = Counter(row["getter_role"] for row in routed)
    risk_counts = Counter(row.get("risk_bucket") or "" for row in routed)
    role_x_route_counts = Counter(f"{row['getter_role']} | {row['route']}" for row in routed)
    safe_split_count = route_status_counts.get("split_only_no_candidate", 0)
    if safe_split_count != EXPECTED_SAFE_ROUTE_COUNT:
        raise SystemExit(f"safe split count guard failed: {safe_split_count}")

    safe_routes = sorted(route for route in route_counts if route in ROUTE_BY_ROLE.values())
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "read_only_policy_splitter_dry_run",
        "agent_key": AGENT_KEY,
        "review_path": str(REVIEW_PATH),
        "review_count": len(routed),
        "expected_count": EXPECTED_TOTAL,
        "count_matches_expected": len(routed) == EXPECTED_TOTAL,
        "registry": registry,
        "registry_guard_ok": not registry_reasons,
        "registry_guard_reasons": registry_reasons,
        "route_counts": top_counter(route_counts),
        "route_status_counts": top_counter(route_status_counts),
        "getter_role_counts": top_counter(role_counts),
        "risk_bucket_counts": top_counter(risk_counts),
        "role_x_route_counts": top_counter(role_x_route_counts),
        "safe_split_count": safe_split_count,
        "hold_context_count": route_status_counts.get("hold_context", 0),
        "safe_route_representative_examples": {
            route: representative_examples(routed, route)
            for route in safe_routes
        },
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "next_recommendation": (
            "Review route_faith_getter_religion_name as a narrow read-only route packet, limited to the 63 "
            "split_only_no_candidate items. Do not include the 82 religion_name rows held for parser/architecture."
        ),
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
    summary["output_files"] = {
        "txt": str(txt_path),
        "jsonl": str(jsonl_path),
        "summary": str(summary_path),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_txt(txt_path, summary, routed)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
