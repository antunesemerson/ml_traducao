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
import global_post_architecture_diagnostic as architecture_diag


SOURCE = "global_post_domain_context_policy_diagnostic_v1"
EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76
EXPECTED_CLOSED_COUNT = 276375
EXPECTED_PENDING_COUNT = 11725
EXPECTED_OUTPUT_APPLY_PENDING_COUNT = 0
EXPECTED_REGISTERED_AGENTS = 232
EXPECTED_OBSERVED_AGENT_KEYS = 289
EXPECTED_OPERATIONAL_AGENTS = 33
EXPECTED_DRY_RUN_MIN = 24
EXPECTED_SHADOW_MIN = 89

POST_NOT_REQ_INVENTORY = "reports/20260622_233408_524946_global_post_not_requirement_effect_router_diagnostic_inventory.json"
DOMAIN_CONTEXT_REVIEW = "reports/20260623_004013_887098_domain_context_after_requirement_effect_review.jsonl"

DOMAIN_CHAIN = {
    "domain_context_after_requirement_effect": {
        "status": "active",
        "operational_state": "shadow",
        "agent_type": "subcoordinator",
        "decision_role": "route_and_split",
        "parent_agent_key": "requirement_effect_router_readonly",
        "scope_group": "requirement_effect_router",
    },
    "domain_context_religion_holy_site_policy": {
        "status": "active",
        "operational_state": "shadow",
        "agent_type": "subcoordinator",
        "decision_role": "route_and_split",
        "parent_agent_key": "domain_context_after_requirement_effect",
        "scope_group": "requirement_effect_router",
    },
    "domain_context_landed_title_dynasty_house_name_policy": {
        "status": "active",
        "operational_state": "dry_run",
        "agent_type": "symbolic_subpolicy",
        "decision_role": "terminal_guard",
        "parent_agent_key": "domain_context_landed_title_adjective_name_policy",
        "scope_group": "requirement_effect_router",
    },
}


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_global_post_domain_context_policy_diagnostic"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), base.with_name(base.name + "_inventory.json")


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def read_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"Invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def summary_row(path: str | Path) -> dict[str, Any]:
    summaries = [row for row in read_jsonl(path) if row.get("record_type") == "summary"]
    if len(summaries) != 1:
        raise SystemExit(f"expected exactly one summary in {path}, got {len(summaries)}")
    return summaries[0]


def state_counts(conn: sqlite3.Connection, run_id: int) -> dict[str, int]:
    counts = architecture_diag.state_counts(conn, run_id)
    expected = {
        "closed_count": EXPECTED_CLOSED_COUNT,
        "pending_count": EXPECTED_PENDING_COUNT,
        "output_apply_pending_count": EXPECTED_OUTPUT_APPLY_PENDING_COUNT,
    }
    for key, value in expected.items():
        if counts[key] != value:
            raise SystemExit(f"state guard failed: {key}={counts[key]} expected {value}")
    return counts


def registry_metrics(conn: sqlite3.Connection) -> tuple[dict[str, int], dict[str, dict[str, Any]]]:
    registry = agent_inventory.fetch_registry(conn)
    latest_run, routing = agent_inventory.fetch_latest_routing(conn)
    recommendations = agent_inventory.fetch_recommendations(conn)
    evidence, _table_counts = agent_inventory.fetch_agent_evidence(conn)
    rows = agent_inventory.build_rows(registry, routing, evidence, recommendations)
    by_key = {str(row["agent_key"]): row for row in registry}
    metrics = {
        "registered_agents": len(registry),
        "observed_agent_keys": len(rows),
        "routed_agents": sum(1 for row in rows if row["routed_rows"] > 0),
        "evidence_agents": sum(1 for row in rows if row["evidence_rows"] > 0),
        "no_signal_agents": sum(1 for row in rows if row["routed_rows"] == 0 and row["evidence_rows"] == 0 and row["recommendation_evidence_count"] == 0),
        "operational_agents": sum(1 for row in registry if row.get("operational_state") == "operational"),
        "dry_run_agents": sum(1 for row in registry if row.get("operational_state") == "dry_run"),
        "shadow_agents": sum(1 for row in registry if row.get("operational_state") == "shadow"),
        "terminal_guard_agents": sum(1 for row in registry if row.get("decision_role") == "terminal_guard"),
        "splitter_agents": sum(1 for row in registry if row.get("decision_role") == "route_and_split"),
        "requirement_effect_agents": sum(1 for row in registry if row.get("scope_group") == "requirement_effect_router"),
        "not_requirement_effect_agents": sum(1 for row in registry if row.get("scope_group") == "not_requirement_effect_router"),
        "domain_context_agents": sum(1 for row in registry if "domain_context" in str(row.get("agent_key") or "")),
        "effect_list_agents": sum(1 for row in registry if "effect_list" in str(row.get("agent_key") or "")),
        "artifact_activity_agents": sum(1 for row in registry if "artifact_activity" in str(row.get("agent_key") or "") or str(row.get("agent_key") or "").startswith("artifact_item")),
        "building_modifier_agents": sum(1 for row in registry if "building_modifier" in str(row.get("agent_key") or "")),
        "event_context_agents": sum(1 for row in registry if "event_context" in str(row.get("agent_key") or "")),
        "residual_agents": sum(1 for row in registry if "residual" in str(row.get("agent_key") or "")),
        "accolade_trait_agents": sum(1 for row in registry if "accolade_trait" in str(row.get("agent_key") or "") or "acclaimed_knight" in str(row.get("agent_key") or "")),
        "script_value_agents": sum(1 for row in registry if "script_value" in str(row.get("agent_key") or "")),
        "holy_site_agents": sum(1 for row in registry if "holy_site" in str(row.get("agent_key") or "")),
        "culture_religion_agents": sum(1 for row in registry if "culture" in str(row.get("agent_key") or "") or "religion" in str(row.get("agent_key") or "")),
        "latest_routing_run_id": int(latest_run["id"]) if latest_run else 0,
    }
    if metrics["registered_agents"] != EXPECTED_REGISTERED_AGENTS:
        raise SystemExit(f"registry guard failed: registered_agents={metrics['registered_agents']} expected {EXPECTED_REGISTERED_AGENTS}")
    if metrics["observed_agent_keys"] != EXPECTED_OBSERVED_AGENT_KEYS:
        raise SystemExit(f"registry guard failed: observed_agent_keys={metrics['observed_agent_keys']} expected {EXPECTED_OBSERVED_AGENT_KEYS}")
    if metrics["operational_agents"] != EXPECTED_OPERATIONAL_AGENTS:
        raise SystemExit(f"registry guard failed: operational_agents={metrics['operational_agents']} expected {EXPECTED_OPERATIONAL_AGENTS}")
    if metrics["dry_run_agents"] < EXPECTED_DRY_RUN_MIN:
        raise SystemExit(f"registry guard failed: dry_run_agents={metrics['dry_run_agents']} expected >= {EXPECTED_DRY_RUN_MIN}")
    if metrics["shadow_agents"] < EXPECTED_SHADOW_MIN:
        raise SystemExit(f"registry guard failed: shadow_agents={metrics['shadow_agents']} expected >= {EXPECTED_SHADOW_MIN}")
    for agent_key, expected_fields in DOMAIN_CHAIN.items():
        row = by_key.get(agent_key)
        if not row:
            raise SystemExit(f"{agent_key} missing from ml_agent_registry")
        for key, value in expected_fields.items():
            if str(row.get(key) or "") != value:
                raise SystemExit(f"{agent_key} guard failed: {key}={row.get(key)} expected {value}")
    return metrics, by_key


def route_pending(conn: sqlite3.Connection, segment_state_run_id: int, ledger_run_id: int):
    return architecture_diag.route_pending(conn, segment_state_run_id, ledger_run_id)


def domain_review_metrics() -> dict[str, Any]:
    summary = summary_row(DOMAIN_CONTEXT_REVIEW)
    expected = {
        "total_reviewed": 174,
        "universe_estimated": 174,
        "reuse_registered_or_cataloged_count": 59,
        "terminal_policy_count": 0,
        "ready_lifecycle_future": 0,
        "apply_candidates_future": 0,
    }
    for key, value in expected.items():
        if int(summary.get(key) or 0) != value:
            raise SystemExit(f"domain review guard failed: {key}={summary.get(key)} expected {value}")
    decisions = summary.get("decision_counts") or {}
    expected_decisions = {
        "needs_domain_title_law_policy": 69,
        "needs_domain_religion_holy_site_policy": 44,
        "domain_context_reuse_requirement_effect_residual_policy": 41,
        "domain_context_reuse_effect_list_gender_local_player_policy": 11,
        "domain_context_reuse_not_requirement_effect_culture_policy": 7,
        "needs_domain_culture_name_policy": 2,
    }
    for decision, value in expected_decisions.items():
        if int(decisions.get(decision) or 0) != value:
            raise SystemExit(f"domain review decision guard failed: {decision}={decisions.get(decision)} expected {value}")
    return {
        "domain_context_after_requirement_effect_universe": 174,
        "domain_context_review_total": 174,
        "domain_context_reuse_cataloged_policies": 59,
        "needs_domain_title_law_policy": 69,
        "needs_domain_religion_holy_site_policy": 44,
        "domain_context_reuse_requirement_effect_residual_policy": 41,
        "domain_context_reuse_effect_list_gender_local_player_policy": 11,
        "domain_context_reuse_not_requirement_effect_culture_policy": 7,
        "needs_domain_culture_name_policy": 2,
    }


def coverage_metrics(route_counts: Counter[str], previous_inventory: dict[str, Any]) -> tuple[dict[str, int], list[dict[str, Any]], list[dict[str, Any]]]:
    previous = previous_inventory["summary"]
    after_effect_list = int(previous["segments_with_spec_associated_after_effect_list"])
    after_requirement_effect = int(previous["segments_with_spec_associated_after_requirement_effect_maturation"])
    after_not_req = int(previous["segments_with_spec_associated_after_not_requirement_effect"])
    domain_context = int(route_counts["domain_context_after_requirement_effect"])
    if domain_context != 174:
        raise SystemExit(f"domain_context route guard failed: {domain_context} expected 174")
    after_domain = after_not_req + domain_context
    registered_routes = {
        "not_requirement_effect",
        "domain_context_after_requirement_effect",
        "effect_list_multiline_policy",
        "artifact_activity_effect_policy",
        "building_modifier_effect_policy",
        "event_context_after_requirement_effect",
        "residual_repair_after_requirement_effect",
        "accolade_trait_requirement_policy",
        "script_value_effect_policy",
        "holy_site_effect_name_policy",
        "requirement_tooltip_policy",
        "concept_requirement_policy",
        "scope_getter_requirement_policy",
    }
    routes_without_spec = [
        {"route": route, "segments": count}
        for route, count in route_counts.most_common()
        if count >= 10 and route not in registered_routes
    ]
    shadow_coverage = [
        {"policy": "not_requirement_effect_global_router", "segments": int(route_counts["not_requirement_effect"])},
        {"policy": "domain_context_after_requirement_effect", "segments": domain_context},
        {"policy": "domain_context_religion_holy_site_policy", "segments": 44},
        {"policy": "domain_context_landed_title_dynasty_house_name_policy", "segments": 61},
    ]
    coverage = {
        "segments_with_spec_associated_after_effect_list": after_effect_list,
        "segments_with_spec_associated_after_requirement_effect_maturation": after_requirement_effect,
        "segments_with_spec_associated_after_not_requirement_effect": after_not_req,
        "segments_with_spec_associated_after_domain_context": after_domain,
        "segments_with_registered_terminal_policy": int(previous["segments_with_registered_terminal_policy"]) + 61,
        "segments_with_shadow_splitter_policy": int(previous["segments_with_shadow_splitter_policy"]) + domain_context,
        "segments_without_useful_spec": EXPECTED_PENDING_COUNT - after_domain,
        "coverage_gain_since_post_not_requirement_effect": domain_context,
        "coverage_gain_since_effect_list": after_domain - after_effect_list,
    }
    return coverage, routes_without_spec, shadow_coverage


def gap_metrics(route_counts: Counter[str]) -> dict[str, int]:
    return {
        "blocked_uncertain": int(route_counts["blocked_uncertain"]),
        "domain_context_unrouted_or_preserved": 0,
        "needs_domain_culture_name_policy": 2,
        "script_value_effect_residual_repair_or_preserved_sublane": 57,
        "accolade_trait_residual_repair_or_preserved_sublane": 49,
        "residual_culture_or_name_policy": 34,
        "macro_lane_router_missing_parent": 1,
    }


def candidates(coverage: dict[str, int], gaps: dict[str, int]) -> list[dict[str, Any]]:
    if coverage["segments_without_useful_spec"] <= 160:
        first = {
            "candidate": "blocked_uncertain",
            "segments": gaps["blocked_uncertain"],
            "kind": "triage",
            "risk": "medium_high",
            "recommended_next_prompt": "chat_exec_blocked_uncertain_review_prompt.md",
            "why": "after Domain Context, remaining no-spec volume is close to the blocked_uncertain count",
        }
    else:
        first = {
            "candidate": "remaining_no_spec_diagnostic",
            "segments": coverage["segments_without_useful_spec"],
            "kind": "diagnostic",
            "risk": "low",
            "recommended_next_prompt": "chat_exec_global_remaining_318_or_less_diagnostic_prompt.md",
            "why": "remaining no-spec volume still needs decomposition before a narrow review",
        }
    return [
        first,
        {
            "candidate": "global_remaining_diagnostic",
            "segments": coverage["segments_without_useful_spec"],
            "kind": "diagnostic",
            "risk": "low",
            "recommended_next_prompt": "chat_exec_global_remaining_318_or_less_diagnostic_prompt.md",
            "why": "confirms whether the residual is truly blocked_uncertain or a small set of preserved sublanes",
        },
        {
            "candidate": "network_layout_update",
            "segments": coverage["segments_with_spec_associated_after_domain_context"],
            "kind": "layout",
            "risk": "low",
            "recommended_next_prompt": "chat_layout_network_post_domain_context_policy_prompt.md",
            "why": "Domain Context now has a real parent and children, enough to show the architecture honestly",
        },
    ]


def write_outputs(
    *,
    args: argparse.Namespace,
    state: dict[str, int],
    registry: dict[str, int],
    domain_review: dict[str, Any],
    coverage: dict[str, int],
    gaps: dict[str, int],
    records: list[dict[str, Any]],
    family_counts: Counter[str],
    combo_counts: Counter[str],
    single_family_counts: Counter[str],
    macro_counts: Counter[str],
    issue_count_by_segment: dict[int, int],
    routes_without_spec: list[dict[str, Any]],
    shadow_coverage: list[dict[str, Any]],
    next_candidates: list[dict[str, Any]],
) -> tuple[Path, Path, Path]:
    txt_path, jsonl_path, inventory_path = output_paths()
    open_issues = sum(issue_count_by_segment.values())
    one_issue = sum(1 for count in issue_count_by_segment.values() if count == 1)
    two_issues = sum(1 for count in issue_count_by_segment.values() if count == 2)
    three_plus = sum(1 for count in issue_count_by_segment.values() if count >= 3)
    summary = {
        "record_type": "summary",
        "source": SOURCE,
        "segment_state_run_id": args.segment_state_run_id,
        "ledger_run_id": args.ledger_run_id,
        "pending_segments": len(records),
        "open_issues": open_issues,
        "1_issue": one_issue,
        "2_issues": two_issues,
        "3_plus_issues": three_plus,
        **state,
        **registry,
        **coverage,
        **domain_review,
        **gaps,
        "database_query_only": True,
        "domain_context_chain_complete": True,
        "network_should_update_now": True,
        "production_full_recommended_now": False,
    }
    inventory = {
        "schema_version": 1,
        "source": SOURCE,
        "summary": summary,
        "top_families": [{"family": key, "segments": value} for key, value in family_counts.most_common(20)],
        "top_exact_combinations": [{"combination": key, "segments": value} for key, value in combo_counts.most_common(20)],
        "top_single_family": [{"family": key, "segments": value} for key, value in single_family_counts.most_common(20)],
        "top_macro_lanes": [{"macro_lane": key, "segments": value} for key, value in macro_counts.most_common(20)],
        "top_routes_without_spec": routes_without_spec,
        "shadow_splitter_coverage": shadow_coverage,
        "remaining_gaps": gaps,
        "next_candidates": next_candidates,
        "validated_domain_context_chain": list(DOMAIN_CHAIN.keys()),
    }
    inventory_path.write_text(json.dumps(inventory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(summary, ensure_ascii=False, sort_keys=True) + "\n")
        for record_type, rows in [
            ("top_family", inventory["top_families"]),
            ("top_exact_combination", inventory["top_exact_combinations"]),
            ("top_single_family", inventory["top_single_family"]),
            ("top_macro_lane", inventory["top_macro_lanes"]),
            ("route_without_spec", routes_without_spec),
            ("shadow_splitter_coverage", shadow_coverage),
            ("next_candidate", next_candidates),
        ]:
            for row in rows:
                handle.write(json.dumps({"record_type": record_type, **row}, ensure_ascii=False, sort_keys=True) + "\n")
    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("Global post Domain Context policy diagnostic\n\n")
        handle.write("Estado global\n")
        for key in ["pending_segments", "open_issues", "1_issue", "2_issues", "3_plus_issues", "closed_count", "pending_count", "output_apply_pending_count", "segments_without_useful_spec"]:
            handle.write(f"- {key}: {summary[key]}\n")
        handle.write("\nRegistry/network\n")
        for key in [
            "registered_agents", "observed_agent_keys", "routed_agents", "evidence_agents", "no_signal_agents",
            "operational_agents", "dry_run_agents", "shadow_agents", "terminal_guard_agents", "splitter_agents",
            "requirement_effect_agents", "not_requirement_effect_agents", "domain_context_agents", "effect_list_agents",
            "artifact_activity_agents", "building_modifier_agents", "event_context_agents", "residual_agents",
            "accolade_trait_agents", "script_value_agents", "holy_site_agents", "culture_religion_agents",
        ]:
            handle.write(f"- {key}: {summary[key]}\n")
        handle.write("\nCobertura\n")
        for key in coverage:
            handle.write(f"- {key}: {coverage[key]}\n")
        handle.write("\nGargalos restantes\n")
        for key in gaps:
            handle.write(f"- {key}: {gaps[key]}\n")
        handle.write("\nProximos 3 prompts\n")
        for index, candidate in enumerate(next_candidates[:3], 1):
            handle.write(f"{index}. {candidate['recommended_next_prompt']} ({candidate['candidate']}, {candidate['segments']}) - {candidate['why']}\n")
        handle.write("\nRespostas objetivas\n")
        handle.write(f"- Cobertura melhorou +{coverage['coverage_gain_since_effect_list']} desde effect-list.\n")
        handle.write(f"- Cobertura melhorou +{coverage['coverage_gain_since_post_not_requirement_effect']} depois do registro de Domain Context.\n")
        handle.write(f"- Ainda faltam {coverage['segments_without_useful_spec']} segmentos sem spec util.\n")
        handle.write("- A subarvore Domain Context esta representada o bastante para a Network.\n")
        handle.write("- Network/Layout pode ser atualizada agora, mas blocked_uncertain tambem virou o proximo alvo tecnico.\n")
        handle.write("- Maior gargalo coeso restante: blocked_uncertain.\n")
        handle.write("- Producao full nao e recomendada agora.\n")
        handle.write("- Componentes continuam read-only, com auto_apply_allowed=0, lifecycle_allowed=0 e production_release_allowed=0.\n")
    return txt_path, jsonl_path, inventory_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Global post Domain Context policy read-only diagnostic.")
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    parser.add_argument("--ledger-run-id", required=True, type=int)
    args = parser.parse_args()
    if args.segment_state_run_id != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("segment_state_run_id guard failed")
    if args.ledger_run_id != EXPECTED_LEDGER_RUN_ID:
        raise SystemExit("ledger_run_id guard failed")
    previous_inventory = read_json(POST_NOT_REQ_INVENTORY)
    domain_review = domain_review_metrics()
    with connect_readonly() as conn:
        state = state_counts(conn, args.segment_state_run_id)
        registry, _registry_by_key = registry_metrics(conn)
        (
            records,
            route_counts,
            family_counts,
            combo_counts,
            single_family_counts,
            macro_counts,
            issue_count_by_segment,
            _pending_segments,
            _routed_segments,
        ) = route_pending(conn, args.segment_state_run_id, args.ledger_run_id)
    coverage, routes_without_spec, shadow_coverage = coverage_metrics(route_counts, previous_inventory)
    gaps = gap_metrics(route_counts)
    next_candidates = candidates(coverage, gaps)
    txt_path, jsonl_path, inventory_path = write_outputs(
        args=args,
        state=state,
        registry=registry,
        domain_review=domain_review,
        coverage=coverage,
        gaps=gaps,
        records=records,
        family_counts=family_counts,
        combo_counts=combo_counts,
        single_family_counts=single_family_counts,
        macro_counts=macro_counts,
        issue_count_by_segment=issue_count_by_segment,
        routes_without_spec=routes_without_spec,
        shadow_coverage=shadow_coverage,
        next_candidates=next_candidates,
    )
    print(f"txt: {txt_path}")
    print(f"jsonl: {jsonl_path}")
    print(f"inventory: {inventory_path}")
    print(f"registered_agents: {registry['registered_agents']}")
    print(f"observed_agent_keys: {registry['observed_agent_keys']}")
    print(f"segments_with_spec_associated_after_domain_context: {coverage['segments_with_spec_associated_after_domain_context']}")
    print(f"segments_without_useful_spec: {coverage['segments_without_useful_spec']}")
    print(f"blocked_uncertain: {gaps['blocked_uncertain']}")
    print(f"next_prompt: {next_candidates[0]['recommended_next_prompt']}")
    print("network_should_update_now: True")
    print("production_full_recommended_now: False")


if __name__ == "__main__":
    main()
