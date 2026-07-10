from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "medium_dynamic_light_architecture_parser_routing_dry_run_v1"
INPUT_PATH = Path("reports/20260630_000234_966579_domain_policy_vote_candidate_medium_dynamic_light_architecture_parser_packet.jsonl")
INPUT_SUMMARY = Path("reports/20260630_000234_966579_domain_policy_vote_candidate_medium_dynamic_light_architecture_parser_packet_summary.json")
GETTER_POLICY = "medium_dynamic_light_getter_perspective_omitted_policy"
PARENT_POLICY = "medium_dynamic_light_residual_parser_policy"
EXPECTED_TOTAL = 10

ROUTE_BY_GROUP = {
    "getter_perspective_omitted": "route_getter_perspective_omitted_policy",
    "SelectLocalization/Select_CString": "hold_selectlocalization_structural_parser",
    "PantheonTerm": "hold_pantheonterm_agreement",
    "relation_or_possessive": "hold_relation_or_perspective",
}

TARGET_POLICY_BY_GROUP = {
    "getter_perspective_omitted": GETTER_POLICY,
    "SelectLocalization/Select_CString": "select_localization_parser_or_structural_hold",
    "PantheonTerm": "explicit_pantheonterm_agreement_hold",
    "relation_or_possessive": "relation_perspective_hold",
}


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


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


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def fetch_registry(conn: sqlite3.Connection, agent_key: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM ml_agent_registry WHERE agent_key = ?", (agent_key,)).fetchone()
    if row is None:
        return {"exists": False, "agent_key": agent_key}
    payload = dict(row)
    notes = json.loads(payload.get("notes_json") or "{}")
    return {
        "exists": True,
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
    }


def validate_inputs(summary: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    if summary.get("mode") != "read_only_architecture_parser_packet":
        raise SystemExit("summary mode guard failed")
    if int(summary.get("packet_count") or 0) != EXPECTED_TOTAL or len(rows) != EXPECTED_TOTAL:
        raise SystemExit("packet count guard failed")
    if summary.get("production_full_recommended_now") is not False:
        raise SystemExit("production_full_recommended_now guard failed")
    if summary.get("source_changed") is not False or summary.get("output_changed") is not False:
        raise SystemExit("source/output changed guard failed")


def route_row(row: dict[str, Any]) -> dict[str, Any]:
    group = str(row.get("architecture_group") or "outros")
    route = ROUTE_BY_GROUP.get(group, "hold_other_architecture_parser_later")
    route_status = "split_only_no_candidate" if group == "getter_perspective_omitted" else "hold_context"
    return {
        "segment_id": int(row["segment_id"]),
        "architecture_group": group,
        "route": route,
        "route_status": route_status,
        "target_policy": TARGET_POLICY_BY_GROUP.get(group, "explicit_hold"),
        "parent_policy": PARENT_POLICY,
        "hold_family": row.get("hold_family"),
        "surface_bucket": row.get("surface_bucket"),
        "source_key": row.get("source_key"),
        "relative_path": row.get("relative_path"),
        "dynamic_tokens": row.get("dynamic_tokens") or [],
        "english_text": row.get("english_text"),
        "spanish_text": row.get("spanish_text"),
        "current_output_text": row.get("current_output_text"),
        "candidate_generation_allowed": False,
        "auto_apply_allowed": False,
        "lifecycle_allowed": False,
        "production_release_allowed": False,
        "requires_architecture_decision": group != "getter_perspective_omitted",
    }


def top_counter(counter: Counter[str]) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in counter.most_common()]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def write_txt(path: Path, summary: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    lines = [
        "medium_dynamic_light architecture parser routing dry-run",
        "",
        f"input_path: {summary['input_path']}",
        f"review_count: {summary['review_count']}",
        f"registry_guard_ok: {str(summary['registry_guard_ok']).lower()}",
        "",
        "route_counts:",
    ]
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["route_counts"])
    lines.extend(["", "target_policy_counts:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["target_policy_counts"])
    lines.extend(["", "items:"])
    for row in rows:
        lines.extend(
            [
                "",
                f"## segment_id {row['segment_id']} | {row['architecture_group']} -> {row['route']}",
                f"- route_status: {row['route_status']}",
                f"- target_policy: {row['target_policy']}",
                f"- source_key: {row['source_key']}",
                f"- dynamic_tokens: {json.dumps(row['dynamic_tokens'], ensure_ascii=False)}",
                f"- output_ptbr: {row['current_output_text']}",
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
    summary_in = read_json(INPUT_SUMMARY)
    rows_in = read_jsonl(INPUT_PATH)
    validate_inputs(summary_in, rows_in)
    with connect_readonly() as conn:
        getter_registry = fetch_registry(conn, GETTER_POLICY)
        parent_registry = fetch_registry(conn, PARENT_POLICY)

    registry_reasons: list[str] = []
    if not getter_registry.get("exists"):
        registry_reasons.append("missing_getter_policy_registry")
    for key in ("candidate_generation_allowed", "auto_apply_allowed", "lifecycle_allowed", "production_release_allowed"):
        if getter_registry.get(key) is not False:
            registry_reasons.append(f"getter_policy_{key}_not_false")
    if getter_registry.get("parent_agent_key") != PARENT_POLICY:
        registry_reasons.append("getter_policy_parent_mismatch")
    if not parent_registry.get("exists"):
        registry_reasons.append("missing_parent_policy_registry")

    routed = [route_row(row) for row in rows_in]
    routed.sort(key=lambda row: (row["route_status"], row["route"], row["segment_id"]))
    route_counts = Counter(row["route"] for row in routed)
    group_counts = Counter(row["architecture_group"] for row in routed)
    status_counts = Counter(row["route_status"] for row in routed)
    target_policy_counts = Counter(row["target_policy"] for row in routed)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "read_only_architecture_parser_routing_dry_run",
        "input_path": str(INPUT_PATH),
        "input_summary": str(INPUT_SUMMARY),
        "review_count": len(routed),
        "expected_count": EXPECTED_TOTAL,
        "count_matches_expected": len(routed) == EXPECTED_TOTAL,
        "getter_policy_registry": getter_registry,
        "parent_policy_registry": parent_registry,
        "registry_guard_ok": not registry_reasons,
        "registry_guard_reasons": registry_reasons,
        "architecture_group_counts": top_counter(group_counts),
        "route_counts": top_counter(route_counts),
        "route_status_counts": top_counter(status_counts),
        "target_policy_counts": top_counter(target_policy_counts),
        "getter_perspective_omitted_to_new_policy_count": int(route_counts["route_getter_perspective_omitted_policy"]),
        "selectlocalization_hold_count": int(route_counts["hold_selectlocalization_structural_parser"]),
        "pantheonterm_hold_count": int(route_counts["hold_pantheonterm_agreement"]),
        "relation_or_possessive_hold_count": int(route_counts["hold_relation_or_perspective"]),
        "candidate_generation_count": 0,
        "apply_output_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "single_operational_recommendation": "Keep this as read-only routing: 5 getter perspective omissions are delegated to the new shadow policy; 2 SelectLocalization, 2 PantheonTerm, and 1 relation/possessive remain explicit holds/parser inputs.",
        "output_files": {},
    }
    base = reports_dir() / f"{stamp()}_medium_dynamic_light_architecture_parser_routing_dry_run"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    write_jsonl(jsonl_path, routed)
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary_json": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_txt(txt_path, summary, routed)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
