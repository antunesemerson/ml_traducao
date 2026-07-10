from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "medium_dynamic_light_residual_parser_policy_dry_run_v1"
AGENT_KEY = "medium_dynamic_light_residual_parser_policy"
INPUT_PATH = Path("reports/20260629_201655_489642_domain_policy_vote_candidate_medium_dynamic_light_residual_architecture_packet.jsonl")
EXPECTED_TOTAL = 81
ROUTE_BY_FAMILY = {
    "generic_getter_role_unknown": "route_residual_generic_getter_role_unknown",
    "article_preposition_title_faith_culture": "route_residual_article_preposition_title_faith_culture",
    "clergy_adherent_role_agreement": "route_residual_clergy_adherent_role_agreement",
    "culture_collective": "route_residual_culture_collective",
    "getter_perspective_omitted": "route_residual_getter_perspective_omitted",
    "pantheonterm_agreement": "route_residual_pantheonterm_agreement",
    "select_localization_select_cstring": "route_residual_select_localization_select_cstring",
    "residual_medium_dynamic_light_human": "hold_residual_medium_dynamic_light_human",
}
HOLD_FAMILIES = {"residual_medium_dynamic_light_human"}


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
        "route_families": notes.get("route_families") or {},
        "hold_families": notes.get("hold_families") or [],
        "family_counts": notes.get("family_counts") or {},
        "packet_count": int(notes.get("packet_count") or 0),
    }


def validate_registry(registry: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    expected = {
        "agent_type": "subcoordinator",
        "status": "active",
        "operational_state": "shadow",
        "decision_role": "route_and_split",
        "parent_agent_key": "medium_dynamic_light_getter_role_policy",
        "scope_group": "domain_policy_vote_candidate",
        "dashboard_group": "Issue Network",
        "packet_count": EXPECTED_TOTAL,
    }
    for key, value in expected.items():
        if registry.get(key) != value:
            reasons.append(f"registry_{key}_mismatch")
    for key in ("candidate_generation_allowed", "auto_apply_allowed", "lifecycle_allowed", "production_release_allowed"):
        if registry.get(key) is not False:
            reasons.append(f"registry_{key}_not_false")
    if registry.get("route_families") != {k: v for k, v in ROUTE_BY_FAMILY.items() if k not in HOLD_FAMILIES}:
        reasons.append("registry_route_families_mismatch")
    if set(registry.get("hold_families") or []) != HOLD_FAMILIES:
        reasons.append("registry_hold_families_mismatch")
    return reasons


def route_row(row: dict[str, Any]) -> dict[str, Any]:
    family = str(row.get("architecture_family") or "")
    route = ROUTE_BY_FAMILY.get(family, "hold_residual_unknown_architecture_family")
    hold = family in HOLD_FAMILIES or route.startswith("hold_")
    return {
        "segment_id": int(row["segment_id"]),
        "agent_key": AGENT_KEY,
        "architecture_family": family,
        "recommended_handling": row.get("recommended_handling"),
        "route": route,
        "route_status": "hold_context" if hold else "split_only_no_candidate",
        "candidate_generation_allowed": False,
        "auto_apply_allowed": False,
        "lifecycle_allowed": False,
        "production_release_allowed": False,
        "requires_human_context": hold or row.get("recommended_handling") in {"human_review", "human_or_parser_policy"},
        "surface_bucket": row.get("surface_bucket"),
        "source_key": row.get("source_key"),
        "relative_path": row.get("relative_path"),
        "dynamic_tokens": row.get("dynamic_tokens") or [],
        "architecture_tags": row.get("architecture_tags") or [],
        "family_reason": row.get("family_reason"),
        "family_recommendation": row.get("family_recommendation"),
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
        "medium_dynamic_light_residual_parser_policy dry-run",
        "",
        f"agent_key: {AGENT_KEY}",
        f"input_path: {summary['input_path']}",
        f"review_count: {summary['review_count']}",
        f"registry_guard_ok: {str(summary['registry_guard_ok']).lower()}",
        "",
        "route_counts:",
    ]
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["route_counts"])
    lines.extend(["", "architecture_family_counts:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["architecture_family_counts"])
    lines.extend(["", "route_status_counts:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["route_status_counts"])
    lines.extend(["", "items:"])
    for row in rows:
        lines.extend(
            [
                "",
                f"## segment_id {row['segment_id']} | {row['architecture_family']} -> {row['route']}",
                f"- route_status: {row['route_status']}",
                f"- recommended_handling: {row.get('recommended_handling')}",
                f"- surface_bucket: {row.get('surface_bucket')}",
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
    source_rows = read_jsonl(INPUT_PATH)
    if len(source_rows) != EXPECTED_TOTAL:
        raise SystemExit(f"input count guard failed: {len(source_rows)} expected {EXPECTED_TOTAL}")
    with connect_readonly() as conn:
        registry = fetch_registry(conn)
    registry_reasons = validate_registry(registry)
    routed = [route_row(row) for row in source_rows]
    routed.sort(key=lambda row: (str(row["route_status"]), str(row["route"]), str(row["surface_bucket"]), int(row["segment_id"])))

    segment_ids = [int(row["segment_id"]) for row in routed]
    if len(segment_ids) != len(set(segment_ids)):
        raise SystemExit("duplicate segment_id guard failed")

    family_counts = Counter(row["architecture_family"] for row in routed)
    route_counts = Counter(row["route"] for row in routed)
    status_counts = Counter(row["route_status"] for row in routed)
    handling_counts = Counter(str(row.get("recommended_handling") or "") for row in routed)
    surface_route_counts = Counter(f"{row.get('surface_bucket')} | {row.get('route')}" for row in routed)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "read_only_residual_policy_splitter_dry_run",
        "agent_key": AGENT_KEY,
        "input_path": str(INPUT_PATH),
        "review_count": len(routed),
        "expected_count": EXPECTED_TOTAL,
        "count_matches_expected": len(routed) == EXPECTED_TOTAL,
        "registry": registry,
        "registry_guard_ok": not registry_reasons,
        "registry_guard_reasons": registry_reasons,
        "architecture_family_counts": top_counter(family_counts),
        "route_counts": top_counter(route_counts),
        "route_status_counts": top_counter(status_counts),
        "handling_counts": top_counter(handling_counts),
        "surface_route_counts": top_counter(surface_route_counts),
        "split_only_no_candidate_count": status_counts.get("split_only_no_candidate", 0),
        "hold_context_count": status_counts.get("hold_context", 0),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "next_recommendation": "Send residual route counts to architecture; keep candidate generation disabled until parser-policy guards are materialized.",
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
        "summary_json": str(summary_path),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_txt(txt_path, summary, routed)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
