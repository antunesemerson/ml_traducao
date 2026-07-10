from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "medium_dynamic_light_generic_getter_absorption_dry_run_v1"
SUMMARY_PATH = Path("reports/20260629_212517_035822_domain_policy_vote_candidate_medium_dynamic_light_residual_generic_getter_review_summary.json")
JSONL_PATH = Path("reports/20260629_212517_035822_domain_policy_vote_candidate_medium_dynamic_light_residual_generic_getter_review.jsonl")
EXPECTED_COUNT = 26
ARTICLE_POLICY = "medium_dynamic_light_article_preposition_policy"
GETTER_POLICY = "medium_dynamic_light_getter_role_policy"


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


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


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


def fetch_registry(conn: sqlite3.Connection, agent_key: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM ml_agent_registry WHERE agent_key = ?", (agent_key,)).fetchone()
    if row is None:
        return {"agent_key": agent_key, "exists": False}
    payload = dict(row)
    notes = json.loads(payload.get("notes_json") or "{}")
    return {
        "agent_key": payload.get("agent_key"),
        "exists": True,
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
    }


def validate_registry(registry: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if not registry.get("exists"):
        return ["missing_registry"]
    expected = {
        "agent_type": "subcoordinator",
        "status": "active",
        "operational_state": "shadow",
        "decision_role": "route_and_split",
        "scope_group": "domain_policy_vote_candidate",
        "dashboard_group": "Issue Network",
    }
    for key, value in expected.items():
        if registry.get(key) != value:
            reasons.append(f"{registry.get('agent_key')}_{key}_mismatch")
    for key in ("candidate_generation_allowed", "auto_apply_allowed", "lifecycle_allowed", "production_release_allowed"):
        if registry.get(key) is not False:
            reasons.append(f"{registry.get('agent_key')}_{key}_not_false")
    return reasons


def validate_inputs(summary: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    if summary.get("mode") != "read_only_route_review":
        raise SystemExit("summary mode guard failed")
    if summary.get("target_route") != "route_residual_generic_getter_role_unknown":
        raise SystemExit("target_route guard failed")
    if int(summary.get("review_count") or 0) != EXPECTED_COUNT or len(rows) != EXPECTED_COUNT:
        raise SystemExit("review_count guard failed")
    if summary.get("count_matches_expected") is not True:
        raise SystemExit("count_matches_expected guard failed")
    counts = Counter(str(row.get("absorption_recommendation") or "") for row in rows)
    expected = {"article_preposition_policy": 14, "getter_role_policy": 11, "hold": 1}
    if dict(counts) != expected:
        raise SystemExit(f"absorption counts guard failed: {dict(counts)} expected {expected}")
    ids = [int(row["segment_id"]) for row in rows]
    if len(ids) != len(set(ids)):
        raise SystemExit("duplicate segment_id guard failed")


def absorb_row(row: dict[str, Any]) -> dict[str, Any]:
    recommendation = str(row.get("absorption_recommendation") or "")
    role = str(row.get("grammar_role_review") or "")
    if recommendation == "article_preposition_policy":
        target_policy = ARTICLE_POLICY
        target_route = "route_article_preposition_absorbed_from_generic_getter"
        status = "split_only_no_candidate"
    elif recommendation == "getter_role_policy":
        target_policy = GETTER_POLICY
        target_route = f"route_getter_role_absorbed_{role}"
        status = "split_only_no_candidate"
    else:
        target_policy = "hold_context"
        target_route = "hold_relation_or_possessive_from_generic_getter"
        status = "hold_context"
    return {
        "segment_id": int(row["segment_id"]),
        "source_route": "route_residual_generic_getter_role_unknown",
        "target_policy": target_policy,
        "target_route": target_route,
        "route_status": status,
        "grammar_role_review": role,
        "absorption_recommendation": recommendation,
        "candidate_generation_allowed": False,
        "auto_apply_allowed": False,
        "lifecycle_allowed": False,
        "production_release_allowed": False,
        "surface_bucket": row.get("surface_bucket"),
        "source_key": row.get("source_key"),
        "relative_path": row.get("relative_path"),
        "dynamic_tokens": row.get("dynamic_tokens") or [],
        "role_tags": row.get("role_tags") or [],
        "role_recommendation": row.get("role_recommendation"),
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


def write_txt(path: Path, summary: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    lines = [
        "medium_dynamic_light generic getter absorption dry-run",
        "",
        f"review_count: {summary['review_count']}",
        f"registry_guard_ok: {str(summary['registry_guard_ok']).lower()}",
        "",
        "absorbed_policy_counts:",
    ]
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["absorbed_policy_counts"])
    lines.extend(["", "target_route_counts:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["target_route_counts"])
    lines.extend(["", "items:"])
    for row in rows:
        lines.extend(
            [
                "",
                f"## segment_id {row['segment_id']} | {row['grammar_role_review']} -> {row['target_policy']}",
                f"- target_route: {row['target_route']}",
                f"- route_status: {row['route_status']}",
                f"- source_key: {row.get('source_key')}",
                f"- dynamic_tokens: {', '.join(row.get('dynamic_tokens') or [])}",
                f"- current_output_text: {row.get('current_output_text')}",
            ]
        )
    lines.extend(
        [
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
    summary_in = read_json(SUMMARY_PATH)
    source_rows = read_jsonl(JSONL_PATH)
    validate_inputs(summary_in, source_rows)
    with connect_readonly() as conn:
        article_registry = fetch_registry(conn, ARTICLE_POLICY)
        getter_registry = fetch_registry(conn, GETTER_POLICY)
    registry_reasons = validate_registry(article_registry) + validate_registry(getter_registry)
    rows = [absorb_row(row) for row in source_rows]
    rows.sort(key=lambda row: (str(row["target_policy"]), str(row["target_route"]), int(row["segment_id"])))

    policy_counts = Counter(str(row["target_policy"]) for row in rows)
    target_route_counts = Counter(str(row["target_route"]) for row in rows)
    status_counts = Counter(str(row["route_status"]) for row in rows)
    role_counts = Counter(str(row["grammar_role_review"]) for row in rows)
    out_summary = {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "read_only_absorption_dry_run",
        "input_summary_path": str(SUMMARY_PATH),
        "input_jsonl_path": str(JSONL_PATH),
        "review_count": len(rows),
        "expected_count": EXPECTED_COUNT,
        "count_matches_expected": len(rows) == EXPECTED_COUNT,
        "registry_guard_ok": not registry_reasons,
        "registry_guard_reasons": registry_reasons,
        "registries": {
            ARTICLE_POLICY: article_registry,
            GETTER_POLICY: getter_registry,
        },
        "absorbed_policy_counts": top_counter(policy_counts),
        "target_route_counts": top_counter(target_route_counts),
        "route_status_counts": top_counter(status_counts),
        "grammar_role_counts": top_counter(role_counts),
        "generic_getter_role_unknown_active_after_absorption": False,
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
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
    base = reports_dir() / f"{stamp()}_{SOURCE}"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    write_jsonl(jsonl_path, rows)
    out_summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary_json": str(summary_path)}
    summary_path.write_text(json.dumps(out_summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_txt(txt_path, out_summary, rows)
    print(json.dumps(out_summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
