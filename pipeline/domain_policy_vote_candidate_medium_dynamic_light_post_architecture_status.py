from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "medium_dynamic_light_post_architecture_status_v1"
ROUTING_SUMMARY = Path("reports/20260630_001739_361277_medium_dynamic_light_architecture_parser_routing_dry_run_summary.json")
GETTER_REVIEW_SUMMARY = Path("reports/20260630_003933_488544_medium_dynamic_light_getter_perspective_omitted_review_summary.json")
GETTER_NOTES_SUMMARY = Path("reports/20260630_005206_505548_medium_dynamic_light_getter_perspective_omitted_policy_notes_update_apply_summary.json")
SELECT_REVIEW_SUMMARY = Path("reports/20260630_010318_976484_medium_dynamic_light_selectlocalization_structural_review_summary.json")
SELECT_REGISTRY_SUMMARY = Path("reports/20260630_011231_292666_medium_dynamic_light_selectlocalization_affix_policy_registry_apply_summary.json")
HOLDS_CLOSEOUT_SUMMARY = Path("reports/20260630_011730_903583_medium_dynamic_light_final_holds_closeout_summary.json")
EXPECTED_ARCHITECTURE_PACKET_COUNT = 10


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


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
    }


def validate_zero_actions(label: str, summary: dict[str, Any]) -> None:
    for key in ("candidate_generation_count", "apply_output_count", "lifecycle_count", "segment_state_count", "reindex_count", "production_full_count"):
        if int(summary.get(key) or 0) != 0:
            raise SystemExit(f"{label} {key} guard failed")
    if summary.get("source_changed") is not False or summary.get("output_changed") is not False:
        raise SystemExit(f"{label} source/output changed guard failed")
    if summary.get("production_full_recommended_now") is not False:
        raise SystemExit(f"{label} production_full_recommended_now guard failed")


def top_counter(counter: Counter[str]) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in counter.most_common()]


def write_txt(path: Path, summary: dict[str, Any]) -> None:
    lines = [
        "medium_dynamic_light post-architecture status",
        "",
        f"architecture_packet_count: {summary['architecture_packet_count']}",
        f"shadow_policy_registered_count: {summary['shadow_policy_registered_count']}",
        f"read_only_ok_or_fluency_count: {summary['read_only_ok_or_fluency_count']}",
        f"explicit_hold_count: {summary['explicit_hold_count']}",
        "",
        "outcome_counts:",
    ]
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["outcome_counts"])
    lines.extend(["", "policies:"])
    for policy in summary["policies"]:
        lines.append(
            f"- {policy['agent_key']} [{policy.get('operational_state')}/{policy.get('decision_role')}], "
            f"candidate_generation={policy.get('candidate_generation_allowed')}"
        )
    lines.extend(
        [
            "",
            "remaining_holds:",
            "- PantheonTerm: 2 | explicit_hold_until_pantheonterm_number_policy",
            "- relation_or_possessive: 1 | hold_collect_more_relation_perspective_signal",
            "",
            f"recommendation: {summary['single_operational_recommendation']}",
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
    routing = read_json(ROUTING_SUMMARY)
    getter_review = read_json(GETTER_REVIEW_SUMMARY)
    getter_notes = read_json(GETTER_NOTES_SUMMARY)
    select_review = read_json(SELECT_REVIEW_SUMMARY)
    select_registry = read_json(SELECT_REGISTRY_SUMMARY)
    holds = read_json(HOLDS_CLOSEOUT_SUMMARY)

    for label, payload in (
        ("routing", routing),
        ("getter_review", getter_review),
        ("getter_notes", getter_notes),
        ("select_review", select_review),
        ("select_registry", select_registry),
        ("holds", holds),
    ):
        validate_zero_actions(label, payload)

    if int(routing.get("review_count") or 0) != EXPECTED_ARCHITECTURE_PACKET_COUNT:
        raise SystemExit("routing review_count guard failed")
    if int(getter_review.get("review_count") or 0) != 5:
        raise SystemExit("getter review_count guard failed")
    if int(select_review.get("review_count") or 0) != 2:
        raise SystemExit("select review_count guard failed")
    if int(holds.get("hold_count") or 0) != 3:
        raise SystemExit("holds count guard failed")

    with connect_readonly() as conn:
        policies = [
            fetch_registry(conn, "medium_dynamic_light_getter_perspective_omitted_policy"),
            fetch_registry(conn, "medium_dynamic_light_selectlocalization_affix_policy"),
        ]
        latest_state = conn.execute(
            """
            SELECT id, finished_at, closed_count, pending_count, reopen_count, output_apply_pending_count
            FROM segment_state_runs
            WHERE finished_at IS NOT NULL
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

    if any(not policy.get("exists") for policy in policies):
        raise SystemExit(f"policy registry guard failed: {policies}")
    if any(policy.get("candidate_generation_allowed") is not False for policy in policies):
        raise SystemExit("policy candidate_generation guard failed")

    outcome_counts = Counter(
        {
            "shadow_policy_route_and_split": 7,
            "read_only_ok_or_fluency": int(getter_review["output_preserves_meaning_count"]),
            "explicit_hold": int(holds["hold_count"]),
        }
    )
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "read_only_post_architecture_status",
        "input_summaries": {
            "routing": str(ROUTING_SUMMARY),
            "getter_review": str(GETTER_REVIEW_SUMMARY),
            "getter_notes": str(GETTER_NOTES_SUMMARY),
            "select_review": str(SELECT_REVIEW_SUMMARY),
            "select_registry": str(SELECT_REGISTRY_SUMMARY),
            "holds": str(HOLDS_CLOSEOUT_SUMMARY),
        },
        "architecture_packet_count": EXPECTED_ARCHITECTURE_PACKET_COUNT,
        "shadow_policy_registered_count": len(policies),
        "read_only_ok_or_fluency_count": int(getter_review["output_preserves_meaning_count"]),
        "selectlocalization_shadow_policy_count": int(select_review["review_count"]),
        "explicit_hold_count": int(holds["hold_count"]),
        "outcome_counts": top_counter(outcome_counts),
        "policies": policies,
        "remaining_hold_counts": holds.get("hold_group_counts"),
        "latest_segment_state": dict(latest_state) if latest_state else None,
        "candidate_generation_count": 0,
        "apply_output_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "single_operational_recommendation": (
            "Close medium_dynamic_light residual architecture micro-block as safe and non-applicable now: "
            "2 shadow policies registered, 5 getter-perspective cases classified as output-preserving/fluency, "
            "and 3 cases parked as explicit holds. Return to broader domain_policy_vote_candidate diagnostics rather than apply or production."
        ),
        "output_files": {},
    }

    base = reports_dir() / f"{stamp()}_medium_dynamic_light_post_architecture_status"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    jsonl_path.write_text(json.dumps(summary, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary_json": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_txt(txt_path, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
