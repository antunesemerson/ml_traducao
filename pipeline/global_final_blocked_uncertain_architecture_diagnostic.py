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
EXPECTED_REGISTERED_AGENTS = 236
EXPECTED_OBSERVED_AGENT_KEYS = 293
EXPECTED_TERMINAL_GUARD_AGENTS = 22
EXPECTED_OPERATIONAL_AGENTS = 33
EXPECTED_DRY_RUN_AGENTS = 28
EXPECTED_ISSUE_NETWORK_AGENTS = 72

BASE_COVERAGE_BEFORE_BLOCKED = 11581
BASE_WITHOUT_USEFUL_SPEC = 144
PROJECTED_TERMINAL_GAIN = 109
PROJECTED_COVERAGE = 11690
PROJECTED_WITHOUT_USEFUL_SPEC = 35

BASE_BLOCKED_JSONL = Path("reports/20260623_123342_502844_blocked_uncertain_review.jsonl")
DOCTRINE_GROUP_JSONL = Path("reports/20260623_130814_897985_blocked_uncertain_religion_culture_faith_doctrine_doctrine_tenet_doctrine_group_review.jsonl")
FAITH_NAME_JSONL = Path("reports/20260623_133641_101414_blocked_uncertain_religion_culture_faith_doctrine_faith_name_review.jsonl")
DEBUG_MARKER_JSONL = Path("reports/20260623_142353_831111_blocked_uncertain_token_integrity_debug_marker_review.jsonl")
GENDER_PERSPECTIVE_JSONL = Path("reports/20260623_151613_390666_blocked_uncertain_gender_perspective_review.jsonl")

TERMINAL_AGENTS = {
    "blocked_uncertain_religion_culture_faith_doctrine_doctrine_tenet_doctrine_group_policy": {
        "review_total": 18,
        "terminal_guard_count": 15,
        "required_note": "domain_guard",
    },
    "blocked_uncertain_religion_culture_faith_doctrine_faith_name_policy": {
        "review_total": 17,
        "terminal_guard_count": 17,
        "required_note": "domain_guard",
    },
    "blocked_uncertain_token_integrity_debug_marker_policy": {
        "review_total": 54,
        "terminal_guard_count": 54,
        "required_note": "short_label_guard",
    },
    "blocked_uncertain_gender_perspective_policy": {
        "review_total": 23,
        "terminal_guard_count": 21,
        "required_note": "domain_guard",
    },
}


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_global_final_blocked_uncertain_architecture_diagnostic"
    inventory = reports_dir / f"{stamp}_global_final_blocked_uncertain_architecture_diagnostic_inventory.json"
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


def sample_rows(path: Path) -> list[dict[str, Any]]:
    rows = [row for row in read_jsonl(path) if row.get("record_type") == "sample_review"]
    ids = [int(row["segment_id"]) for row in rows]
    if len(ids) != len(set(ids)):
        raise SystemExit(f"duplicate segment_id in {path}")
    return rows


def require_no_future_flags(rows: list[dict[str, Any]], label: str) -> None:
    apply_count = sum(1 for row in rows if row.get("requires_apply_later") is True)
    lifecycle_count = sum(1 for row in rows if row.get("requires_lifecycle_later") is True)
    if apply_count:
        raise SystemExit(f"{label}: requires_apply_later true count={apply_count}")
    if lifecycle_count:
        raise SystemExit(f"{label}: requires_lifecycle_later true count={lifecycle_count}")


def state_counts(conn: sqlite3.Connection, run_id: int) -> dict[str, int]:
    row = conn.execute(
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
        "closed_count": int(row["closed_count"] or 0),
        "pending_count": int(row["pending_count"] or 0),
        "output_apply_pending_count": int(row["output_apply_pending_count"] or 0),
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
    return {
        "rows": len(rows),
        "pending_ok": sum(1 for row in rows if row["state_group"] == "pending" and int(row["is_closed"] or 0) == 0),
        "needs_output_apply_sum": sum(int(row["needs_output_apply"] or 0) for row in rows),
        "confirmed_matches_output_ok": sum(1 for row in rows if int(row["confirmed_matches_output"] or 0) == 1),
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
        "operational_agents": sum(1 for row in registry if row.get("operational_state") == "operational"),
        "dry_run_agents": sum(1 for row in registry if row.get("operational_state") == "dry_run"),
        "shadow_agents": sum(1 for row in registry if row.get("operational_state") == "shadow"),
        "terminal_guard_agents": sum(1 for row in registry if row.get("decision_role") == "terminal_guard"),
        "splitter_agents": sum(1 for row in registry if row.get("decision_role") == "route_and_split"),
        "issue_network_agents": sum(1 for row in registry if row.get("dashboard_group") == "Issue Network"),
        "latest_routing_run_id": int(latest_run["id"]) if latest_run else 0,
        "table_counts": table_counts,
    }
    expected = {
        "registered_agents": EXPECTED_REGISTERED_AGENTS,
        "observed_agent_keys": EXPECTED_OBSERVED_AGENT_KEYS,
        "operational_agents": EXPECTED_OPERATIONAL_AGENTS,
        "dry_run_agents": EXPECTED_DRY_RUN_AGENTS,
        "terminal_guard_agents": EXPECTED_TERMINAL_GUARD_AGENTS,
        "issue_network_agents": EXPECTED_ISSUE_NETWORK_AGENTS,
    }
    for key, value in expected.items():
        if metrics[key] != value:
            raise SystemExit(f"registry guard failed: {key}={metrics[key]} expected {value}")
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
            and validation["review_total"] == expected["review_total"]
            and validation["terminal_guard_count"] == expected["terminal_guard_count"]
            and validation["required_note_value"] is True
        )
        if not validation["valid"]:
            raise SystemExit(f"terminal registry validation failed: {validation}")
        validations.append(validation)
    return validations


def terminal_ids() -> dict[str, set[int]]:
    doctrine = {
        int(row["segment_id"])
        for row in sample_rows(DOCTRINE_GROUP_JSONL)
        if row.get("doctrine_group_decision") == "doctrine_group_terminal_guard_with_domain_guard"
    }
    faith = {
        int(row["segment_id"])
        for row in sample_rows(FAITH_NAME_JSONL)
        if row.get("faith_name_decision") == "faith_name_terminal_guard_with_domain_guard"
    }
    debug = {
        int(row["segment_id"])
        for row in sample_rows(DEBUG_MARKER_JSONL)
        if row.get("debug_marker_decision") == "debug_marker_terminal_guard_with_short_label_guard"
    }
    gender = {int(row["segment_id"]) for row in sample_rows(GENDER_PERSPECTIVE_JSONL)}
    expected = {"doctrine_group": 15, "faith_name": 17, "debug_marker": 54, "gender_perspective": 23}
    actual = {"doctrine_group": len(doctrine), "faith_name": len(faith), "debug_marker": len(debug), "gender_perspective": len(gender)}
    for key, value in expected.items():
        if actual[key] != value:
            raise SystemExit(f"terminal id guard failed: {key}={actual[key]} expected {value}")
    return {"doctrine_group": doctrine, "faith_name": faith, "debug_marker": debug, "gender_perspective": gender}


def calculate_remaining() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    base_samples = sample_rows(BASE_BLOCKED_JSONL)
    if len(base_samples) != BASE_WITHOUT_USEFUL_SPEC:
        raise SystemExit(f"base blocked review guard failed: {len(base_samples)} expected {BASE_WITHOUT_USEFUL_SPEC}")
    for path, label in [
        (BASE_BLOCKED_JSONL, "blocked_uncertain base"),
        (DOCTRINE_GROUP_JSONL, "doctrine group"),
        (FAITH_NAME_JSONL, "faith name"),
        (DEBUG_MARKER_JSONL, "debug marker"),
        (GENDER_PERSPECTIVE_JSONL, "gender perspective"),
    ]:
        require_no_future_flags(sample_rows(path), label)

    decision_ids: dict[str, set[int]] = {}
    for row in base_samples:
        decision_ids.setdefault(str(row.get("blocked_decision")), set()).add(int(row["segment_id"]))
    terminals = terminal_ids()
    terminal_union = set().union(*terminals.values())
    if len(terminal_union) != PROJECTED_TERMINAL_GAIN:
        raise SystemExit(f"projected terminal gain guard failed: {len(terminal_union)} expected {PROJECTED_TERMINAL_GAIN}")

    religion_leftover = decision_ids.get("needs_blocked_religion_culture_policy", set()) - terminals["doctrine_group"] - terminals["faith_name"]
    token_leftover = decision_ids.get("needs_blocked_token_integrity_policy", set()) - terminals["debug_marker"]
    gender_leftover = decision_ids.get("needs_blocked_gender_perspective_policy", set()) - terminals["gender_perspective"]
    language = decision_ids.get("needs_blocked_language_residual_policy", set())
    name_title = decision_ids.get("needs_blocked_name_title_culture_policy", set())
    explained = terminal_union | religion_leftover | token_leftover | gender_leftover | language | name_title
    unexpected = {int(row["segment_id"]) for row in base_samples} - explained

    remaining = [
        {
            "record_type": "remaining_sublane",
            "sublane": "blocked_religion_culture_leftovers_projected",
            "count": len(religion_leftover),
            "recommended_prompt": "chat_exec_blocked_uncertain_religion_culture_policy_catalog_registration_prompt.md",
        },
        {
            "record_type": "remaining_sublane",
            "sublane": "needs_blocked_language_residual_policy",
            "count": len(language),
            "recommended_prompt": "phase_resolution_planning_no_apply",
        },
        {
            "record_type": "remaining_sublane",
            "sublane": "needs_blocked_name_title_culture_policy",
            "count": len(name_title),
            "recommended_prompt": "phase_resolution_planning_no_apply",
        },
        {
            "record_type": "remaining_sublane",
            "sublane": "blocked_token_integrity_leftovers_projected",
            "count": len(token_leftover),
            "recommended_prompt": "",
        },
        {
            "record_type": "remaining_sublane",
            "sublane": "blocked_gender_perspective_leftovers_projected",
            "count": len(gender_leftover),
            "recommended_prompt": "",
        },
        {
            "record_type": "remaining_sublane",
            "sublane": "unexpected_residual_projected",
            "count": len(unexpected),
            "recommended_prompt": "investigate_before_any_parent_registration" if unexpected else "",
        },
    ]
    total_remaining = sum(row["count"] for row in remaining)
    if total_remaining != PROJECTED_WITHOUT_USEFUL_SPEC:
        raise SystemExit(f"remaining guard failed: {total_remaining} expected {PROJECTED_WITHOUT_USEFUL_SPEC}")
    return remaining, {
        "base_total": len(base_samples),
        "terminal_gain": len(terminal_union),
        "terminal_ids": terminals,
        "remaining_ids": religion_leftover | token_leftover | gender_leftover | language | name_title | unexpected,
        "base_ids": {int(row["segment_id"]) for row in base_samples},
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
    dominant = max(remaining, key=lambda row: row["count"])
    next_prompt = "chat_exec_blocked_uncertain_religion_culture_policy_catalog_registration_prompt.md"
    summary = {
        "record_type": "summary",
        "segment_state_run_id": args.segment_state_run_id,
        "ledger_run_id": args.ledger_run_id,
        **state,
        "registered_agents": inventory["registered_agents"],
        "observed_agent_keys": inventory["observed_agent_keys"],
        "operational_agents": inventory["operational_agents"],
        "dry_run_agents": inventory["dry_run_agents"],
        "shadow_agents": inventory["shadow_agents"],
        "terminal_guard_agents": inventory["terminal_guard_agents"],
        "splitter_agents": inventory["splitter_agents"],
        "issue_network_agents": inventory["issue_network_agents"],
        "coverage_before_blocked": BASE_COVERAGE_BEFORE_BLOCKED,
        "blocked_terminal_gain_projected": PROJECTED_TERMINAL_GAIN,
        "coverage_after_blocked_terminals_projected": PROJECTED_COVERAGE,
        "segments_without_useful_spec_projected": PROJECTED_WITHOUT_USEFUL_SPEC,
        "dominant_remaining_sublane": dominant["sublane"],
        "dominant_remaining_count": dominant["count"],
        "probably_incomplete_routing_count": PROJECTED_WITHOUT_USEFUL_SPEC,
        "true_blocked_count": 0,
        "needs_blocked_religion_culture_parent": True,
        "needs_blocked_parent_router": True,
        "register_religion_culture_parent_first": True,
        "register_blocked_parent_now": False,
        "network_update_now": False,
        "production_full_recommended": False,
        "resolution_phase_ready_for_planning": True,
        "next_prompt": next_prompt,
    }
    inventory_payload = {
        "summary": summary,
        "registry_validations": registry_validations,
        "remaining_sublanes": remaining,
        "pending_validation": pending_validation,
    }

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for key in [
            "coverage_before_blocked",
            "blocked_terminal_gain_projected",
            "coverage_after_blocked_terminals_projected",
            "segments_without_useful_spec_projected",
            "true_blocked_count",
        ]:
            handle.write(json.dumps({"record_type": "summary", "metric": key, "value": summary[key]}, ensure_ascii=False, sort_keys=True) + "\n")
        handle.write(json.dumps(summary, ensure_ascii=False, sort_keys=True) + "\n")
        for validation in registry_validations:
            handle.write(json.dumps(validation, ensure_ascii=False, sort_keys=True) + "\n")
        for row in remaining:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        handle.write(json.dumps({"record_type": "pending_validation", **pending_validation}, ensure_ascii=False, sort_keys=True) + "\n")

    inventory_path.write_text(json.dumps(inventory_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    lines = [
        "global final blocked_uncertain architecture diagnostic",
        f"segment_state_run_id={args.segment_state_run_id}",
        f"ledger_run_id={args.ledger_run_id}",
        "",
        "registry/network:",
        f"- registered_agents: {summary['registered_agents']}",
        f"- observed_agent_keys: {summary['observed_agent_keys']}",
        f"- operational/dry_run/shadow: {summary['operational_agents']}/{summary['dry_run_agents']}/{summary['shadow_agents']}",
        f"- terminal_guard_agents: {summary['terminal_guard_agents']}",
        f"- splitter_agents: {summary['splitter_agents']}",
        f"- issue_network_agents: {summary['issue_network_agents']}",
        "",
        "coverage:",
        f"- before blocked: {BASE_COVERAGE_BEFORE_BLOCKED}",
        f"- blocked terminal gain projected: +{PROJECTED_TERMINAL_GAIN}",
        f"- after blocked terminals projected: {PROJECTED_COVERAGE}",
        f"- segments without useful spec projected: {PROJECTED_WITHOUT_USEFUL_SPEC}",
        "",
        "remaining sublanes:",
        *[f"- {row['sublane']}: {row['count']}" for row in remaining],
        "",
        f"probably_incomplete_routing_count={summary['probably_incomplete_routing_count']}",
        "true_blocked_count=0",
        "blocked_uncertain_parent_router_needed=true",
        "register_religion_culture_parent_first=true",
        "register_blocked_parent_now=false",
        "network_update_now=false",
        "production_full_recommended=false",
        f"next_prompt={next_prompt}",
        "",
        "resolution phase notes:",
        "- registrar parent religion/culture antes do parent global blocked_uncertain.",
        "- depois registrar/validar parent blocked_uncertain como router read-only se os 35 restantes continuarem explicados.",
        "- fase de resolucao deve comecar com resolvers dry-run, sem producao full ate parent/router ficar consolidado.",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, inventory_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Final read-only diagnostic for blocked_uncertain architecture.")
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    parser.add_argument("--ledger-run-id", required=True, type=int)
    args = parser.parse_args()
    if args.segment_state_run_id != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit(f"segment_state_run_id guard failed: {args.segment_state_run_id}")
    if args.ledger_run_id != EXPECTED_LEDGER_RUN_ID:
        raise SystemExit(f"ledger_run_id guard failed: {args.ledger_run_id}")

    remaining, id_info = calculate_remaining()
    with connect_readonly() as conn:
        state = state_counts(conn, args.segment_state_run_id)
        inventory, _rows = registry_inventory(conn)
        registry_validations = validate_terminal_registry(conn)
        pending_validation = validate_pending(conn, id_info["base_ids"], args.segment_state_run_id)
    if pending_validation["rows"] != BASE_WITHOUT_USEFUL_SPEC or pending_validation["pending_ok"] != BASE_WITHOUT_USEFUL_SPEC:
        raise SystemExit(f"pending validation guard failed: {pending_validation}")
    if pending_validation["needs_output_apply_sum"] != 0 or pending_validation["confirmed_matches_output_ok"] != BASE_WITHOUT_USEFUL_SPEC:
        raise SystemExit(f"state detail validation guard failed: {pending_validation}")

    txt_path, jsonl_path, inventory_path = write_outputs(
        args=args,
        state=state,
        inventory=inventory,
        registry_validations=registry_validations,
        remaining=remaining,
        pending_validation=pending_validation,
    )
    print(f"txt: {txt_path}")
    print(f"jsonl: {jsonl_path}")
    print(f"inventory: {inventory_path}")
    print(f"coverage_before_blocked: {BASE_COVERAGE_BEFORE_BLOCKED}")
    print(f"blocked_terminal_gain_projected: {PROJECTED_TERMINAL_GAIN}")
    print(f"coverage_after_blocked_terminals_projected: {PROJECTED_COVERAGE}")
    print(f"segments_without_useful_spec_projected: {PROJECTED_WITHOUT_USEFUL_SPEC}")
    print("next_prompt: chat_exec_blocked_uncertain_religion_culture_policy_catalog_registration_prompt.md")


if __name__ == "__main__":
    main()
