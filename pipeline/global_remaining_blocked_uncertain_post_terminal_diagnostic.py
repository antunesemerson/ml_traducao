from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import agent_inventory_diagnostic as agent_inventory
import db


EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76
EXPECTED_CLOSED_COUNT = 276375
EXPECTED_PENDING_COUNT = 11725
EXPECTED_OUTPUT_APPLY_PENDING_COUNT = 0
EXPECTED_REGISTERED_AGENTS = 235
EXPECTED_OBSERVED_AGENT_KEYS = 292

BASE_COVERAGE_AFTER_DOMAIN_CONTEXT = 11581
BASE_WITHOUT_USEFUL_SPEC = 144
PROJECTED_TERMINAL_GAIN = 86
PROJECTED_COVERAGE = 11667
PROJECTED_WITHOUT_USEFUL_SPEC = 58

BASE_BLOCKED_JSONL = Path("reports/20260623_123342_502844_blocked_uncertain_review.jsonl")
DOCTRINE_GROUP_JSONL = Path("reports/20260623_130814_897985_blocked_uncertain_religion_culture_faith_doctrine_doctrine_tenet_doctrine_group_review.jsonl")
FAITH_NAME_JSONL = Path("reports/20260623_133641_101414_blocked_uncertain_religion_culture_faith_doctrine_faith_name_review.jsonl")
DEBUG_MARKER_JSONL = Path("reports/20260623_142353_831111_blocked_uncertain_token_integrity_debug_marker_review.jsonl")

TERMINAL_AGENTS = {
    "blocked_uncertain_religion_culture_faith_doctrine_doctrine_tenet_doctrine_group_policy": {
        "expected_count": 15,
        "required_note": "domain_guard",
    },
    "blocked_uncertain_religion_culture_faith_doctrine_faith_name_policy": {
        "expected_count": 17,
        "required_note": "domain_guard",
    },
    "blocked_uncertain_token_integrity_debug_marker_policy": {
        "expected_count": 54,
        "required_note": "short_label_guard",
    },
}


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_global_remaining_blocked_uncertain_post_terminal_diagnostic"
    inventory = reports_dir / f"{stamp}_global_remaining_blocked_uncertain_post_terminal_diagnostic_inventory.json"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), inventory


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    full_path = db.project_path(str(path))
    rows: list[dict[str, Any]] = []
    with full_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"Invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def sample_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("record_type") == "sample_review"]


def require_no_future_flags(rows: list[dict[str, Any]], label: str) -> None:
    apply_count = sum(1 for row in rows if row.get("requires_apply_later") is True)
    lifecycle_count = sum(1 for row in rows if row.get("requires_lifecycle_later") is True)
    if apply_count:
        raise SystemExit(f"{label}: requires_apply_later true count={apply_count}")
    if lifecycle_count:
        raise SystemExit(f"{label}: requires_lifecycle_later true count={lifecycle_count}")


def state_counts(conn: sqlite3.Connection, run_id: int) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT
            SUM(CASE WHEN state_group = 'closed' THEN 1 ELSE 0 END) AS closed_count,
            SUM(CASE WHEN state_group = 'pending' THEN 1 ELSE 0 END) AS pending_count,
            SUM(CASE WHEN needs_output_apply = 1 THEN 1 ELSE 0 END) AS output_apply_pending_count
        FROM segment_state_items
        WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()
    counts = {
        "closed_count": int(rows["closed_count"] or 0),
        "pending_count": int(rows["pending_count"] or 0),
        "output_apply_pending_count": int(rows["output_apply_pending_count"] or 0),
    }
    expected = {
        "closed_count": EXPECTED_CLOSED_COUNT,
        "pending_count": EXPECTED_PENDING_COUNT,
        "output_apply_pending_count": EXPECTED_OUTPUT_APPLY_PENDING_COUNT,
    }
    for key, value in expected.items():
        if counts[key] != value:
            raise SystemExit(f"state guard failed: {key}={counts[key]} expected {value}")
    return counts


def validate_pending(conn: sqlite3.Connection, segment_ids: set[int], run_id: int) -> dict[str, int]:
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT segment_id, state_group, is_closed, needs_output_apply, confirmed_matches_output
        FROM segment_state_items
        WHERE run_id = ?
          AND segment_id IN ({placeholders})
        """,
        (run_id, *sorted(segment_ids)),
    ).fetchall()
    pending_ok = sum(1 for row in rows if row["state_group"] == "pending" and int(row["is_closed"] or 0) == 0)
    needs_output_apply = sum(int(row["needs_output_apply"] or 0) for row in rows)
    confirmed_ok = sum(1 for row in rows if int(row["confirmed_matches_output"] or 0) == 1)
    return {
        "rows": len(rows),
        "pending_ok": pending_ok,
        "needs_output_apply_sum": needs_output_apply,
        "confirmed_matches_output_ok": confirmed_ok,
        "missing_state_rows": len(segment_ids) - len(rows),
    }


def registry_inventory(conn: sqlite3.Connection) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    registry = agent_inventory.fetch_registry(conn)
    latest_run, routing = agent_inventory.fetch_latest_routing(conn)
    recommendations = agent_inventory.fetch_recommendations(conn)
    evidence, table_counts = agent_inventory.fetch_agent_evidence(conn)
    rows = agent_inventory.build_rows(registry, routing, evidence, recommendations)
    metrics = {
        "registered_agents": len(registry),
        "observed_agent_keys": len(rows),
        "routed_agents": len(routing),
        "evidence_agents": len(evidence),
        "no_signal_agents": sum(1 for row in rows if not row.get("has_registry") and not row.get("has_routing") and not row.get("has_evidence")),
        "active_operational_agents": sum(1 for row in registry if row.get("status") == "active" and row.get("operational_state") == "operational"),
        "active_dry_run_agents": sum(1 for row in registry if row.get("status") == "active" and row.get("operational_state") == "dry_run"),
        "active_shadow_agents": sum(1 for row in registry if row.get("status") == "active" and row.get("operational_state") == "shadow"),
        "terminal_guard_agents": sum(1 for row in registry if row.get("decision_role") == "terminal_guard"),
        "splitter_agents": sum(1 for row in registry if row.get("decision_role") == "route_and_split"),
        "latest_routing_run_id": int(latest_run["id"]) if latest_run else 0,
        "table_counts": table_counts,
    }
    if metrics["registered_agents"] != EXPECTED_REGISTERED_AGENTS:
        raise SystemExit(f"registered_agents guard failed: {metrics['registered_agents']} expected {EXPECTED_REGISTERED_AGENTS}")
    if metrics["observed_agent_keys"] != EXPECTED_OBSERVED_AGENT_KEYS:
        raise SystemExit(f"observed_agent_keys guard failed: {metrics['observed_agent_keys']} expected {EXPECTED_OBSERVED_AGENT_KEYS}")
    return metrics, rows


def validate_terminal_registry(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    validations: list[dict[str, Any]] = []
    for agent_key, expected in TERMINAL_AGENTS.items():
        row = conn.execute("SELECT * FROM ml_agent_registry WHERE agent_key = ?", (agent_key,)).fetchone()
        if row is None:
            raise SystemExit(f"missing terminal agent: {agent_key}")
        notes = json.loads(row["notes_json"] or "{}")
        validation = {
            "record_type": "registry_validation",
            "agent_key": agent_key,
            "status": row["status"],
            "operational_state": row["operational_state"],
            "agent_type": row["agent_type"],
            "decision_role": row["decision_role"],
            "scope_group": row["scope_group"],
            "dashboard_group": row["dashboard_group"],
            "auto_apply_allowed": int(notes.get("auto_apply_allowed") or 0),
            "production_release_allowed": int(notes.get("production_release_allowed") or 0),
            "lifecycle_allowed": int(notes.get("lifecycle_allowed") or 0),
            "review_total": int(notes.get("review_total") or 0),
            "terminal_guard_count": int(notes.get("terminal_guard_count") or 0),
            "required_note": expected["required_note"],
            "required_note_value": bool(notes.get(expected["required_note"])),
        }
        validation["valid"] = (
            validation["status"] == "active"
            and validation["operational_state"] == "dry_run"
            and validation["agent_type"] == "symbolic_subpolicy"
            and validation["decision_role"] == "terminal_guard"
            and validation["auto_apply_allowed"] == 0
            and validation["production_release_allowed"] == 0
            and validation["lifecycle_allowed"] == 0
            and validation["terminal_guard_count"] == expected["expected_count"]
            and validation["required_note_value"] is True
        )
        if not validation["valid"]:
            raise SystemExit(f"terminal registry validation failed: {validation}")
        validations.append(validation)
    return validations


def terminal_ids() -> dict[str, set[int]]:
    doctrine = {
        int(row["segment_id"])
        for row in sample_rows(read_jsonl(DOCTRINE_GROUP_JSONL))
        if row.get("debug_marker_decision") != "never"
        and row.get("doctrine_group_decision") == "doctrine_group_terminal_guard_with_domain_guard"
    }
    faith = {
        int(row["segment_id"])
        for row in sample_rows(read_jsonl(FAITH_NAME_JSONL))
        if row.get("faith_name_decision") == "faith_name_terminal_guard_with_domain_guard"
    }
    debug = {
        int(row["segment_id"])
        for row in sample_rows(read_jsonl(DEBUG_MARKER_JSONL))
        if row.get("debug_marker_decision") == "debug_marker_terminal_guard_with_short_label_guard"
    }
    if len(doctrine) != 15 or len(faith) != 17 or len(debug) != 54:
        raise SystemExit(f"terminal id guard failed: doctrine={len(doctrine)} faith={len(faith)} debug={len(debug)}")
    return {"doctrine_group": doctrine, "faith_name": faith, "debug_marker": debug}


def calculate_remaining() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    base_samples = sample_rows(read_jsonl(BASE_BLOCKED_JSONL))
    require_no_future_flags(base_samples, "blocked_uncertain base")
    decision_ids: dict[str, set[int]] = {}
    for row in base_samples:
        decision_ids.setdefault(str(row.get("blocked_decision")), set()).add(int(row["segment_id"]))
    terminals = terminal_ids()
    terminal_union = set().union(*terminals.values())
    if len(terminal_union) != PROJECTED_TERMINAL_GAIN:
        raise SystemExit(f"projected terminal gain guard failed: {len(terminal_union)} expected {PROJECTED_TERMINAL_GAIN}")

    religion_leftover = decision_ids.get("needs_blocked_religion_culture_policy", set()) - terminals["doctrine_group"] - terminals["faith_name"]
    token_leftover = decision_ids.get("needs_blocked_token_integrity_policy", set()) - terminals["debug_marker"]
    remaining = [
        {
            "record_type": "remaining_sublane",
            "sublane": "blocked_religion_culture_leftovers_projected",
            "count": len(religion_leftover),
            "recommended_prompt": "chat_exec_blocked_uncertain_religion_culture_policy_catalog_registration_prompt.md",
        },
        {
            "record_type": "remaining_sublane",
            "sublane": "needs_blocked_gender_perspective_policy",
            "count": len(decision_ids.get("needs_blocked_gender_perspective_policy", set())),
            "recommended_prompt": "chat_exec_blocked_uncertain_gender_perspective_review_prompt.md",
        },
        {
            "record_type": "remaining_sublane",
            "sublane": "needs_blocked_language_residual_policy",
            "count": len(decision_ids.get("needs_blocked_language_residual_policy", set())),
            "recommended_prompt": "chat_exec_global_remaining_blocked_uncertain_post_terminal_diagnostic_prompt.md",
        },
        {
            "record_type": "remaining_sublane",
            "sublane": "needs_blocked_name_title_culture_policy",
            "count": len(decision_ids.get("needs_blocked_name_title_culture_policy", set())),
            "recommended_prompt": "chat_exec_global_remaining_blocked_uncertain_post_terminal_diagnostic_prompt.md",
        },
        {
            "record_type": "remaining_sublane",
            "sublane": "blocked_token_integrity_leftovers_projected",
            "count": len(token_leftover),
            "recommended_prompt": "",
        },
    ]
    total_remaining = sum(row["count"] for row in remaining)
    if total_remaining != PROJECTED_WITHOUT_USEFUL_SPEC:
        raise SystemExit(f"remaining guard failed: {total_remaining} expected {PROJECTED_WITHOUT_USEFUL_SPEC}")
    return remaining, {
        "base_total": len(base_samples),
        "terminal_gain": len(terminal_union),
        "terminal_ids": terminals,
        "remaining_ids": set().union(religion_leftover, token_leftover, *[
            decision_ids.get(name, set())
            for name in [
                "needs_blocked_gender_perspective_policy",
                "needs_blocked_language_residual_policy",
                "needs_blocked_name_title_culture_policy",
            ]
        ]),
    }


def write_outputs(
    *,
    args: argparse.Namespace,
    state: dict[str, int],
    inventory: dict[str, Any],
    registry_validations: list[dict[str, Any]],
    remaining: list[dict[str, Any]],
    pending_validation: dict[str, int],
) -> tuple[Path, Path, Path]:
    txt_path, jsonl_path, inventory_path = output_paths()
    dominant_remaining = max(remaining, key=lambda row: row["count"])
    next_prompt = (
        "chat_exec_blocked_uncertain_gender_perspective_review_prompt.md"
        if next((row["count"] for row in remaining if row["sublane"] == "needs_blocked_gender_perspective_policy"), 0) >= 15
        else "chat_exec_blocked_uncertain_policy_catalog_registration_prompt.md"
    )
    summary = {
        "record_type": "summary",
        "segment_state_run_id": args.segment_state_run_id,
        "ledger_run_id": args.ledger_run_id,
        **state,
        "registered_agents": inventory["registered_agents"],
        "observed_agent_keys": inventory["observed_agent_keys"],
        "active_operational_agents": inventory["active_operational_agents"],
        "active_dry_run_agents": inventory["active_dry_run_agents"],
        "active_shadow_agents": inventory["active_shadow_agents"],
        "terminal_guard_agents": inventory["terminal_guard_agents"],
        "splitter_agents": inventory["splitter_agents"],
        "coverage_before_blocked": BASE_COVERAGE_AFTER_DOMAIN_CONTEXT,
        "blocked_terminal_gain_projected": PROJECTED_TERMINAL_GAIN,
        "coverage_after_blocked_terminals_projected": PROJECTED_COVERAGE,
        "segments_without_useful_spec_projected": PROJECTED_WITHOUT_USEFUL_SPEC,
        "dominant_remaining_sublane": dominant_remaining["sublane"],
        "dominant_remaining_count": dominant_remaining["count"],
        "probably_incomplete_routing_count": PROJECTED_WITHOUT_USEFUL_SPEC,
        "true_blocked_count": 0,
        "needs_blocked_parent_router": True,
        "network_update_now": False,
        "production_full_recommended": False,
        "next_prompt": next_prompt,
    }
    inventory_payload = {
        "summary": summary,
        "registry_validations": registry_validations,
        "remaining_sublanes": remaining,
        "pending_validation": pending_validation,
        "inventory": inventory,
    }
    inventory_path.write_text(json.dumps(inventory_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(summary, ensure_ascii=False, sort_keys=True) + "\n")
        for metric in [
            ("coverage_before_blocked", BASE_COVERAGE_AFTER_DOMAIN_CONTEXT),
            ("blocked_terminal_gain_projected", PROJECTED_TERMINAL_GAIN),
            ("coverage_after_blocked_terminals_projected", PROJECTED_COVERAGE),
            ("segments_without_useful_spec_projected", PROJECTED_WITHOUT_USEFUL_SPEC),
        ]:
            handle.write(json.dumps({"record_type": "summary_metric", "metric": metric[0], "value": metric[1]}, sort_keys=True) + "\n")
        for row in registry_validations:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        for row in remaining:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        handle.write(json.dumps({"record_type": "pending_validation", **pending_validation}, ensure_ascii=False, sort_keys=True) + "\n")
    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("Global remaining blocked_uncertain post-terminal diagnostic\n\n")
        for key in [
            "registered_agents", "observed_agent_keys", "active_operational_agents",
            "active_dry_run_agents", "active_shadow_agents", "terminal_guard_agents",
            "splitter_agents", "coverage_before_blocked", "blocked_terminal_gain_projected",
            "coverage_after_blocked_terminals_projected", "segments_without_useful_spec_projected",
            "dominant_remaining_sublane", "dominant_remaining_count", "true_blocked_count",
            "needs_blocked_parent_router", "network_update_now", "production_full_recommended",
            "next_prompt",
        ]:
            handle.write(f"- {key}: {summary[key]}\n")
        handle.write("\nRemaining sublanes\n")
        for row in remaining:
            handle.write(f"- {row['sublane']}: {row['count']} -> {row['recommended_prompt']}\n")
        handle.write("\nRegistry validations\n")
        for row in registry_validations:
            handle.write(f"- {row['agent_key']}: valid={row['valid']} state={row['operational_state']} role={row['decision_role']}\n")
        handle.write(f"\nPending validation: {json.dumps(pending_validation, ensure_ascii=False, sort_keys=True)}\n")
    return txt_path, jsonl_path, inventory_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--segment-state-run-id", type=int, required=True)
    parser.add_argument("--ledger-run-id", type=int, required=True)
    args = parser.parse_args()
    if args.segment_state_run_id != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit(f"segment-state guard failed: {args.segment_state_run_id}")
    if args.ledger_run_id != EXPECTED_LEDGER_RUN_ID:
        raise SystemExit(f"ledger guard failed: {args.ledger_run_id}")

    for label, path in [
        ("base", BASE_BLOCKED_JSONL),
        ("doctrine_group", DOCTRINE_GROUP_JSONL),
        ("faith_name", FAITH_NAME_JSONL),
        ("debug_marker", DEBUG_MARKER_JSONL),
    ]:
        require_no_future_flags(sample_rows(read_jsonl(path)), label)

    remaining, coverage = calculate_remaining()
    segment_ids = set()
    segment_ids.update(coverage["remaining_ids"])
    for ids in coverage["terminal_ids"].values():
        segment_ids.update(ids)

    with connect_readonly() as conn:
        state = state_counts(conn, args.segment_state_run_id)
        inventory, _rows = registry_inventory(conn)
        registry_validations = validate_terminal_registry(conn)
        pending_validation = validate_pending(conn, segment_ids, args.segment_state_run_id)

    txt_path, jsonl_path, inventory_path = write_outputs(
        args=args,
        state=state,
        inventory=inventory,
        registry_validations=registry_validations,
        remaining=remaining,
        pending_validation=pending_validation,
    )
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"inventory={inventory_path}")


if __name__ == "__main__":
    main()
