from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import agent_inventory_diagnostic as agent_inventory
import db


EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76
EXPECTED_PENDING_TOTAL = 11725
EXPECTED_CLOSED_COUNT = 276375
EXPECTED_OUTPUT_APPLY_PENDING_COUNT = 0
EXPECTED_REGISTERED_AGENTS = 238
EXPECTED_OBSERVED_AGENT_KEYS = 295
EXPECTED_OPERATIONAL_AGENTS = 33
EXPECTED_DRY_RUN_AGENTS = 28
EXPECTED_SHADOW_AGENTS = 91
EXPECTED_SPLITTER_AGENTS = 27
EXPECTED_ISSUE_NETWORK_AGENTS = 74

COVERAGE = {
    "coverage_after_effect_list": 5784,
    "coverage_after_requirement_effect_maturation": 10159,
    "coverage_after_not_requirement_effect": 11407,
    "coverage_after_domain_context": 11581,
    "coverage_after_blocked_uncertain_projected": 11690,
    "segments_without_useful_spec_projected": 35,
    "coverage_gain_since_effect_list": 5906,
    "coverage_gain_since_domain_context": 109,
    "true_blocked_count": 0,
}

REMAINING_SUBLANES = {
    "blocked_religion_culture_leftovers_projected": 28,
    "needs_blocked_language_residual_policy": 6,
    "needs_blocked_name_title_culture_policy": 1,
}

RELIGION_CULTURE_LEFTOVERS = {
    "needs_blocked_religion_culture_tenet_policy": 19,
    "needs_doctrine_group_name_policy": 3,
    "needs_doctrine_tenet_short_label_policy": 2,
    "needs_faith_doctrine_gender_perspective_policy": 1,
    "needs_faith_doctrine_short_label_policy": 1,
    "blocked_religion_culture_terminal_guard": 1,
    "blocked_religion_culture_reuse_domain_context_religion_holy_site_policy": 1,
}

REQUIRED_ACTIVE = [
    "requirement_effect_router_readonly",
    "not_requirement_effect_global_router",
    "blocked_uncertain",
    "domain_context_after_requirement_effect",
    "effect_list_multiline_policy",
    "artifact_activity_effect_policy",
    "building_modifier_effect_policy",
    "event_context_after_requirement_effect",
    "residual_repair_after_requirement_effect",
    "accolade_trait_requirement_policy",
    "script_value_effect_policy",
    "holy_site_effect_name_policy",
    "not_requirement_effect_culture_religion_router",
    "not_requirement_effect_culture_policy",
    "not_requirement_effect_culture_tradition_heritage_policy",
    "blocked_uncertain_religion_culture_policy",
    "blocked_uncertain_religion_culture_faith_doctrine_doctrine_tenet_doctrine_group_policy",
    "blocked_uncertain_religion_culture_faith_doctrine_faith_name_policy",
    "blocked_uncertain_token_integrity_debug_marker_policy",
    "blocked_uncertain_gender_perspective_policy",
]


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_global_final_architecture_before_resolution_diagnostic"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), base.with_name(base.name + "_inventory.json")


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


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
        "pending_count": EXPECTED_PENDING_TOTAL,
        "output_apply_pending_count": EXPECTED_OUTPUT_APPLY_PENDING_COUNT,
    }
    for key, value in expected.items():
        if counts[key] != value:
            raise SystemExit(f"state guard failed: {key}={counts[key]} expected {value}")
    return counts


def inventory_metrics(conn: sqlite3.Connection) -> tuple[dict[str, Any], list[dict[str, Any]]]:
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
        "requirement_effect_agents": sum(1 for row in registry if row.get("scope_group") == "requirement_effect_router"),
        "not_requirement_effect_agents": sum(1 for row in registry if row.get("scope_group") == "not_requirement_effect_router"),
        "blocked_uncertain_agents": sum(1 for row in registry if row.get("scope_group") == "blocked_uncertain_router"),
        "domain_context_agents": sum(1 for row in registry if "domain_context" in str(row.get("agent_key") or "")),
        "latest_routing_run_id": int(latest_run["id"]) if latest_run else 0,
        "table_counts": table_counts,
    }
    expected = {
        "registered_agents": EXPECTED_REGISTERED_AGENTS,
        "observed_agent_keys": EXPECTED_OBSERVED_AGENT_KEYS,
        "operational_agents": EXPECTED_OPERATIONAL_AGENTS,
        "dry_run_agents": EXPECTED_DRY_RUN_AGENTS,
        "shadow_agents": EXPECTED_SHADOW_AGENTS,
        "splitter_agents": EXPECTED_SPLITTER_AGENTS,
        "issue_network_agents": EXPECTED_ISSUE_NETWORK_AGENTS,
    }
    for key, value in expected.items():
        if metrics[key] != value:
            raise SystemExit(f"inventory guard failed: {key}={metrics[key]} expected {value}")
    return metrics, rows


def validate_components(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    validations: list[dict[str, Any]] = []
    for key in REQUIRED_ACTIVE:
        row = conn.execute("SELECT * FROM ml_agent_registry WHERE agent_key = ?", (key,)).fetchone()
        if row is None:
            raise SystemExit(f"missing required component: {key}")
        notes = json.loads(row["notes_json"] or "{}")
        validation = {
            "record_type": "component_validation",
            "agent_key": key,
            "status": row["status"],
            "operational_state": row["operational_state"],
            "agent_type": row["agent_type"],
            "decision_role": row["decision_role"],
            "scope_group": row["scope_group"],
            "dashboard_group": row["dashboard_group"],
            "auto_apply_allowed": int(notes.get("auto_apply_allowed") or 0),
            "production_release_allowed": int(notes.get("production_release_allowed") or 0),
            "lifecycle_allowed": int(notes.get("lifecycle_allowed") or 0),
        }
        validation["valid"] = (
            validation["status"] == "active"
            and validation["auto_apply_allowed"] == 0
            and validation["production_release_allowed"] == 0
            and validation["lifecycle_allowed"] == 0
        )
        if not validation["valid"]:
            raise SystemExit(f"component validation failed: {validation}")
        validations.append(validation)
    return validations


def write_outputs(
    *,
    args: argparse.Namespace,
    state: dict[str, int],
    inventory: dict[str, Any],
    validations: list[dict[str, Any]],
) -> tuple[Path, Path, Path]:
    txt_path, jsonl_path, inventory_path = output_paths()
    summary = {
        "record_type": "summary",
        "segment_state_run_id": args.segment_state_run_id,
        "ledger_run_id": args.ledger_run_id,
        "pending_total": EXPECTED_PENDING_TOTAL,
        **state,
        **inventory,
        **COVERAGE,
        "remaining_sublanes": REMAINING_SUBLANES,
        "religion_culture_leftovers": RELIGION_CULTURE_LEFTOVERS,
        "architecture_covers_practically_all_pending": True,
        "network_update_data_only_if_dashboard_stale": True,
        "network_redesign_now": False,
        "production_full_recommended_now": False,
        "train_or_promote_model_now": False,
        "next_prompt": "chat_exec_resolver_dry_run_strategy_prompt.md",
    }
    payload = {
        "schema_version": 1,
        "source": "global_final_architecture_before_resolution_diagnostic_v1",
        "summary": summary,
        "component_validations": validations,
        "remaining_sublanes": REMAINING_SUBLANES,
        "religion_culture_leftovers": RELIGION_CULTURE_LEFTOVERS,
        "resolver_dry_run_start_order": [
            "effect_list_concept_policy / holy_site_effect_name_policy / script_value_effect_policy",
            "blocked_uncertain_token_integrity_debug_marker_policy",
            "domain_context_landed_title_dynasty_house_name_policy",
            "gender_local_player_policy / Select_CString policies",
        ],
    }
    inventory_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(summary, ensure_ascii=False, sort_keys=True) + "\n")
        for key, value in COVERAGE.items():
            handle.write(json.dumps({"record_type": "coverage_metric", "metric": key, "value": value}, ensure_ascii=False, sort_keys=True) + "\n")
        for key, value in REMAINING_SUBLANES.items():
            handle.write(json.dumps({"record_type": "remaining_sublane", "sublane": key, "count": value}, ensure_ascii=False, sort_keys=True) + "\n")
        for key, value in RELIGION_CULTURE_LEFTOVERS.items():
            handle.write(json.dumps({"record_type": "religion_culture_leftover", "sublane": key, "count": value}, ensure_ascii=False, sort_keys=True) + "\n")
        for row in validations:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "global final architecture before resolution diagnostic",
        f"segment_state_run_id={args.segment_state_run_id}",
        f"ledger_run_id={args.ledger_run_id}",
        "",
        "registry/network:",
        f"- registered_agents: {inventory['registered_agents']}",
        f"- observed_agent_keys: {inventory['observed_agent_keys']}",
        f"- operational/dry_run/shadow: {inventory['operational_agents']}/{inventory['dry_run_agents']}/{inventory['shadow_agents']}",
        f"- terminal_guard_agents: {inventory['terminal_guard_agents']}",
        f"- splitter_agents: {inventory['splitter_agents']}",
        f"- Issue Network agents: {inventory['issue_network_agents']}",
        f"- requirement_effect agents: {inventory['requirement_effect_agents']}",
        f"- not_requirement_effect agents: {inventory['not_requirement_effect_agents']}",
        f"- blocked_uncertain agents: {inventory['blocked_uncertain_agents']}",
        f"- domain_context agents: {inventory['domain_context_agents']}",
        "",
        "coverage:",
        f"- coverage_after_effect_list: {COVERAGE['coverage_after_effect_list']}",
        f"- coverage_after_requirement_effect_maturation: {COVERAGE['coverage_after_requirement_effect_maturation']}",
        f"- coverage_after_not_requirement_effect: {COVERAGE['coverage_after_not_requirement_effect']}",
        f"- coverage_after_domain_context: {COVERAGE['coverage_after_domain_context']}",
        f"- coverage_after_blocked_uncertain_projected: {COVERAGE['coverage_after_blocked_uncertain_projected']}/{EXPECTED_PENDING_TOTAL}",
        f"- segments_without_useful_spec_projected: {COVERAGE['segments_without_useful_spec_projected']}",
        f"- coverage_gain_since_effect_list: {COVERAGE['coverage_gain_since_effect_list']}",
        f"- coverage_gain_since_domain_context: {COVERAGE['coverage_gain_since_domain_context']}",
        "- true_blocked_count: 0",
        "",
        "remaining without useful spec:",
        *[f"- {key}: {value}" for key, value in REMAINING_SUBLANES.items()],
        "",
        "religion/culture leftovers:",
        *[f"- {key}: {value}" for key, value in RELIGION_CULTURE_LEFTOVERS.items()],
        "",
        "answers:",
        "1. A arquitetura de roteamento cobre praticamente todas as pendencias: sim, 11690/11725.",
        "2. Restam 35 sem spec util: 28 religion/culture leftovers, 6 language residual, 1 name/title/culture.",
        "3. Producao full agora: nao; ainda falta desenhar resolvers dry-run e medir risco real.",
        "4. Evolucao: iniciar resolvers dry-run por grupos com metricas suggestion_candidate, guarded_no_apply, false_safe_risk, token_integrity_ok e would_change_output.",
        "5. Rede/modelo: sem treino/model promotion agora; manter arquitetura simbolica/router-first e atualizar dashboard apenas se os dados exibidos estiverem defasados.",
        "",
        "recommended resolver order:",
        "- effect_list_concept_policy / holy_site_effect_name_policy / script_value_effect_policy",
        "- blocked_uncertain_token_integrity_debug_marker_policy",
        "- domain_context_landed_title_dynasty_house_name_policy",
        "- gender/local-player e Select_CString em dry-run por risco de perspectiva",
        "",
        "network recommendation: data-only update if dashboard does not show registered_agents=238, shadow_agents=91, dry_run_agents=28, Issue Network=74 and blocked_uncertain metadata; no layout redesign now.",
        "next_prompt=chat_exec_resolver_dry_run_strategy_prompt.md",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, inventory_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Final read-only architecture diagnostic before resolver phase.")
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    parser.add_argument("--ledger-run-id", required=True, type=int)
    args = parser.parse_args()
    if args.segment_state_run_id != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit(f"segment_state_run_id guard failed: {args.segment_state_run_id}")
    if args.ledger_run_id != EXPECTED_LEDGER_RUN_ID:
        raise SystemExit(f"ledger_run_id guard failed: {args.ledger_run_id}")
    with connect_readonly() as conn:
        state = state_counts(conn, args.segment_state_run_id)
        inventory, _rows = inventory_metrics(conn)
        validations = validate_components(conn)
    txt_path, jsonl_path, inventory_path = write_outputs(args=args, state=state, inventory=inventory, validations=validations)
    print(f"txt: {txt_path}")
    print(f"jsonl: {jsonl_path}")
    print(f"inventory: {inventory_path}")
    print(f"coverage_after_blocked_uncertain_projected: {COVERAGE['coverage_after_blocked_uncertain_projected']}")
    print(f"segments_without_useful_spec_projected: {COVERAGE['segments_without_useful_spec_projected']}")
    print("next_prompt: chat_exec_resolver_dry_run_strategy_prompt.md")


if __name__ == "__main__":
    main()
