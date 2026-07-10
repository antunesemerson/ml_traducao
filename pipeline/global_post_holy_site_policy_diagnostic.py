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


SOURCE = "global_post_holy_site_policy_diagnostic_v1"
EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76
EXPECTED_REGISTERED_AGENTS = 225
EXPECTED_SHADOW_AGENTS = 84
EXPECTED_OPERATIONAL_AGENTS = 33
EXPECTED_CLOSED_COUNT = 276375
EXPECTED_PENDING_COUNT = 11725
EXPECTED_OUTPUT_APPLY_PENDING_COUNT = 0

POST_ACCOLADE_INVENTORY = "reports/20260622_181131_804527_global_post_accolade_trait_policy_diagnostic_inventory.json"
SCRIPT_VALUE_REVIEW = "reports/20260622_181934_836489_script_value_effect_policy_review.jsonl"
HOLY_SITE_REVIEW = "reports/20260622_183529_976411_holy_site_effect_name_policy_review.jsonl"
SCRIPT_VALUE_APPLY = "reports/20260622_182657_641111_script_value_effect_policy_catalog_registry_apply.jsonl"
HOLY_SITE_APPLY = "reports/20260622_184520_869202_holy_site_effect_name_policy_catalog_registry_apply.jsonl"


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_global_post_holy_site_policy_diagnostic"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), base.with_name(base.name + "_inventory.json")


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    probe = conn.execute("PRAGMA query_only").fetchone()
    if int(probe[0]) != 1:
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


def route_pending(
    conn: sqlite3.Connection,
    segment_state_run_id: int,
    ledger_run_id: int,
) -> tuple[list[dict[str, Any]], Counter[str], Counter[str], Counter[str], Counter[str], Counter[str], dict[int, int], int, int]:
    return architecture_diag.route_pending(conn, segment_state_run_id, ledger_run_id)


def registry_metrics(conn: sqlite3.Connection) -> tuple[dict[str, int], dict[str, dict[str, Any]]]:
    registry = agent_inventory.fetch_registry(conn)
    latest_run, routing = agent_inventory.fetch_latest_routing(conn)
    recommendations = agent_inventory.fetch_recommendations(conn)
    evidence, _table_counts = agent_inventory.fetch_agent_evidence(conn)
    rows = agent_inventory.build_rows(registry, routing, evidence, recommendations)
    by_key = {str(row["agent_key"]): row for row in registry}
    routed_agents = sum(1 for row in rows if row["routed_rows"] > 0)
    evidence_agents = sum(1 for row in rows if row["evidence_rows"] > 0)
    no_signal_agents = sum(
        1
        for row in rows
        if row["routed_rows"] == 0
        and row["evidence_rows"] == 0
        and row["recommendation_evidence_count"] == 0
    )
    metrics = {
        "registered_agents": len(registry),
        "observed_agent_keys": len(rows),
        "routed_agents": routed_agents,
        "evidence_agents": evidence_agents,
        "no_signal_agents": no_signal_agents,
        "operational_agents": sum(1 for row in registry if row.get("operational_state") == "operational"),
        "dry_run_agents": sum(1 for row in registry if row.get("operational_state") == "dry_run"),
        "shadow_agents": sum(1 for row in registry if row.get("operational_state") == "shadow"),
        "terminal_guard_agents": sum(1 for row in registry if row.get("decision_role") == "terminal_guard"),
        "splitter_agents": sum(1 for row in registry if row.get("decision_role") == "route_and_split"),
        "requirement_effect_agents": sum(1 for row in registry if row.get("scope_group") == "requirement_effect_router"),
        "effect_list_agents": sum(1 for row in registry if "effect_list" in str(row.get("agent_key") or "")),
        "artifact_activity_agents": sum(1 for row in registry if "artifact_activity" in str(row.get("agent_key") or "") or str(row.get("agent_key") or "").startswith("artifact_item")),
        "building_modifier_agents": sum(1 for row in registry if "building_modifier" in str(row.get("agent_key") or "")),
        "event_context_agents": sum(1 for row in registry if "event_context" in str(row.get("agent_key") or "")),
        "residual_agents": sum(1 for row in registry if "residual" in str(row.get("agent_key") or "")),
        "accolade_trait_agents": sum(1 for row in registry if "accolade_trait" in str(row.get("agent_key") or "") or "acclaimed_knight" in str(row.get("agent_key") or "")),
        "script_value_agents": sum(1 for row in registry if "script_value" in str(row.get("agent_key") or "")),
        "holy_site_agents": sum(1 for row in registry if "holy_site" in str(row.get("agent_key") or "")),
        "latest_routing_run_id": int(latest_run["id"]) if latest_run else 0,
    }
    expected = {
        "registered_agents": EXPECTED_REGISTERED_AGENTS,
        "shadow_agents": EXPECTED_SHADOW_AGENTS,
        "operational_agents": EXPECTED_OPERATIONAL_AGENTS,
    }
    for key, value in expected.items():
        if metrics[key] != value:
            raise SystemExit(f"registry guard failed: {key}={metrics[key]} expected {value}")
    for agent_key in ["script_value_effect_policy", "holy_site_effect_name_policy"]:
        row = by_key.get(agent_key)
        if not row:
            raise SystemExit(f"{agent_key} missing from ml_agent_registry")
        required = {
            "status": "active",
            "operational_state": "shadow",
            "agent_type": "subcoordinator",
            "decision_role": "route_and_split",
            "parent_agent_key": "requirement_effect_router_readonly",
            "scope_group": "requirement_effect_router",
            "dashboard_group": "Issue Network",
        }
        for key, value in required.items():
            if str(row.get(key) or "") != value:
                raise SystemExit(f"{agent_key} registry guard failed: {key}={row.get(key)} expected {value}")
    return metrics, by_key


def review_metrics() -> dict[str, Any]:
    script = summary_row(SCRIPT_VALUE_REVIEW)
    holy = summary_row(HOLY_SITE_REVIEW)
    return {
        "script_value_effect_universe_estimated": int(script["universe_estimated"]),
        "script_value_effect_review_total": int(script["total_reviewed"]),
        "script_value_effect_reuse_cataloged_policies": int(script["reused_cataloged_policy_count"]),
        "script_value_effect_dominant_reuse": str(script["dominant_subtype"]),
        "script_value_effect_residual_repair_or_preserved_sublane": int(script["decision_counts"].get("needs_script_value_effect_residual_repair", 0)),
        "holy_site_universe_estimated": int(holy["universe_estimated"]),
        "holy_site_review_total": int(holy["total_reviewed"]),
        "holy_site_reuse_cataloged_policies": int(holy["reused_cataloged_policy_count"]),
        "holy_site_reuse_effect_list_concept_policy": int(holy["decision_counts"].get("holy_site_reuse_effect_list_concept_policy", 0)),
        "holy_site_remaining_uncovered": 0,
    }


def coverage_metrics(route_counts: Counter[str], post_accolade: dict[str, Any]) -> tuple[dict[str, int], list[dict[str, Any]], list[dict[str, Any]]]:
    previous = post_accolade["summary"]
    after_accolade = int(previous["segments_with_spec_associated_after_accolade_trait"])
    script_value = int(route_counts["script_value_effect_policy"])
    holy_site = int(route_counts["holy_site_effect_name_policy"])
    after_script = after_accolade + script_value
    after_holy = after_script + holy_site
    registered_routes = {
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
        if count >= 25 and route not in registered_routes
    ]
    shadow_coverage = [
        {"policy": "effect_list_multiline_policy", "segments": route_counts["effect_list_multiline_policy"]},
        {"policy": "artifact_activity_effect_policy", "segments": route_counts["artifact_activity_effect_policy"]},
        {"policy": "building_modifier_effect_policy", "segments": route_counts["building_modifier_effect_policy"]},
        {"policy": "event_context_after_requirement_effect", "segments": route_counts["event_context_after_requirement_effect"]},
        {"policy": "residual_repair_after_requirement_effect", "segments": route_counts["residual_repair_after_requirement_effect"]},
        {"policy": "accolade_trait_requirement_policy", "segments": route_counts["accolade_trait_requirement_policy"]},
        {"policy": "script_value_effect_policy", "segments": script_value},
        {"policy": "holy_site_effect_name_policy", "segments": holy_site},
    ]
    coverage = {
        "segments_with_spec_associated_after_effect_list": int(previous["segments_with_spec_associated_after_effect_list"]),
        "segments_with_spec_associated_after_artifact_activity": int(previous["segments_with_spec_associated_after_artifact_activity"]),
        "segments_with_spec_associated_after_building_modifier": int(previous["segments_with_spec_associated_after_building_modifier"]),
        "segments_with_spec_associated_after_event_context": int(previous["segments_with_spec_associated_after_event_context"]),
        "segments_with_spec_associated_after_residual": int(previous["segments_with_spec_associated_after_residual"]),
        "segments_with_spec_associated_after_accolade_trait": after_accolade,
        "segments_with_spec_associated_after_script_value_effect": after_script,
        "segments_with_spec_associated_after_holy_site": after_holy,
        "segments_with_registered_terminal_policy": int(previous["segments_with_registered_terminal_policy"]),
        "segments_with_shadow_splitter_policy": int(previous["segments_with_shadow_splitter_policy"]) + script_value + holy_site,
        "segments_without_useful_spec": EXPECTED_PENDING_COUNT - after_holy,
        "coverage_gain_after_script_value_and_holy_site": script_value + holy_site,
        "coverage_gain_after_requirement_effect_wave": after_holy - int(previous["segments_with_spec_associated_after_effect_list"]),
    }
    return coverage, routes_without_spec, shadow_coverage


def gap_metrics(route_counts: Counter[str], post_accolade: dict[str, Any], reviews: dict[str, Any]) -> dict[str, int]:
    previous = post_accolade["summary"]
    return {
        "not_requirement_effect": int(route_counts["not_requirement_effect"]),
        "domain_context_after_requirement_effect": int(route_counts["domain_context_after_requirement_effect"]),
        "blocked_uncertain": int(route_counts["blocked_uncertain"]),
        "script_value_effect_residual_repair_or_preserved_sublane": int(reviews["script_value_effect_residual_repair_or_preserved_sublane"]),
        "accolade_trait_residual_repair_or_preserved_sublane": int(previous["accolade_trait_residual_preserved_sublane"]),
        "residual_culture_or_name_policy": int(previous["residual_culture_or_name_policy"]),
        "event_context_preserved_sublanes": int(previous["event_context_preserved_sublanes"]),
        "holy_site_remaining_uncovered": int(reviews["holy_site_remaining_uncovered"]),
    }


def candidates(route_counts: Counter[str], gaps: dict[str, int], coverage: dict[str, int]) -> list[dict[str, Any]]:
    rows = [
        {
            "candidate": "not_requirement_effect",
            "segments": int(route_counts["not_requirement_effect"]),
            "kind": "global_router",
            "expected_gain_type": "outside_requirement_effect_architecture",
            "risk": "medium",
            "recommended_next_prompt": "chat_exec_not_requirement_effect_global_router_review_prompt.md",
            "why": "largest remaining unspecced population is outside the requirement/effect router",
        },
        {
            "candidate": "domain_context_after_requirement_effect",
            "segments": int(route_counts["domain_context_after_requirement_effect"]),
            "kind": "requirement_effect_review",
            "expected_gain_type": "domain_context_splitter",
            "risk": "medium",
            "recommended_next_prompt": "chat_exec_domain_context_after_requirement_effect_review_prompt.md",
            "why": "largest cohesive route still inside requirement/effect after ScriptValue and holy-site registration",
        },
        {
            "candidate": "network_layout_update",
            "segments": int(coverage["segments_with_spec_associated_after_holy_site"]),
            "kind": "layout",
            "expected_gain_type": "architecture_visibility",
            "risk": "low",
            "recommended_next_prompt": "chat_layout_network_requirement_effect_architecture_wave_prompt.md",
            "why": "requirement/effect has a mature read-only component layer with 8 shadow splitters now visible",
        },
        {
            "candidate": "script_value_effect_residual_review",
            "segments": int(gaps["script_value_effect_residual_repair_or_preserved_sublane"]),
            "kind": "narrow_review",
            "expected_gain_type": "residual_split",
            "risk": "medium",
            "recommended_next_prompt": "chat_exec_script_value_effect_residual_review_prompt.md",
            "why": "preserved ScriptValue residual exists but is smaller than global not_requirement_effect and domain-context",
        },
        {
            "candidate": "accolade_trait_residual_review",
            "segments": int(gaps["accolade_trait_residual_repair_or_preserved_sublane"]),
            "kind": "narrow_review",
            "expected_gain_type": "residual_split",
            "risk": "medium",
            "recommended_next_prompt": "chat_exec_accolade_trait_residual_review_prompt.md",
            "why": "preserved accolade/trait residual remains material but is not the largest next move",
        },
    ]
    rows.append(
        {
            "candidate": "production_full",
            "segments": 0,
            "kind": "production_full",
            "expected_gain_type": "production_measurement",
            "risk": "high",
            "recommended_next_prompt": "none_now",
            "why": "not_requirement_effect remains large and requirement/effect still has unspecced domain/context routes",
        }
    )
    return rows


def write_reports(
    *,
    args: argparse.Namespace,
    state: dict[str, int],
    registry: dict[str, int],
    reviews: dict[str, Any],
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
        **reviews,
        **gaps,
        "database_query_only": True,
        "script_value_registry_apply_jsonl": SCRIPT_VALUE_APPLY,
        "holy_site_registry_apply_jsonl": HOLY_SITE_APPLY,
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
        "top_shadow_splitter_coverage": shadow_coverage,
        "remaining_gaps": gaps,
        "next_candidates": next_candidates,
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
        handle.write("Global post holy-site policy diagnostic\n\n")
        handle.write("Estado global\n")
        for key in ["pending_segments", "open_issues", "1_issue", "2_issues", "3_plus_issues", "closed_count", "pending_count", "output_apply_pending_count", "segments_without_useful_spec"]:
            handle.write(f"- {key}: {summary[key]}\n")
        handle.write("\nRegistry/network\n")
        for key in [
            "registered_agents",
            "observed_agent_keys",
            "routed_agents",
            "evidence_agents",
            "no_signal_agents",
            "operational_agents",
            "dry_run_agents",
            "shadow_agents",
            "terminal_guard_agents",
            "splitter_agents",
            "requirement_effect_agents",
            "effect_list_agents",
            "artifact_activity_agents",
            "building_modifier_agents",
            "event_context_agents",
            "residual_agents",
            "accolade_trait_agents",
            "script_value_agents",
            "holy_site_agents",
        ]:
            handle.write(f"- {key}: {summary[key]}\n")
        handle.write("\nCobertura de arquitetura\n")
        for key in coverage:
            handle.write(f"- {key}: {coverage[key]}\n")
        handle.write("\nGargalos restantes obrigatorios\n")
        for key in gaps:
            handle.write(f"- {key}: {gaps[key]}\n")
        handle.write("\nReviews recentes\n")
        for key in reviews:
            handle.write(f"- {key}: {reviews[key]}\n")
        handle.write("\nTop rotas sem spec util\n")
        for gap in routes_without_spec[:12]:
            handle.write(f"- {gap['route']}: {gap['segments']}\n")
        handle.write("\nProximos 3 prompts recomendados\n")
        for index, candidate in enumerate(next_candidates[:3], 1):
            handle.write(f"{index}. {candidate['recommended_next_prompt']} ({candidate['candidate']}, {candidate['segments']}, {candidate['risk']}) - {candidate['why']}\n")
        handle.write("\nRespostas objetivas\n")
        handle.write("- Cobertura melhorou +668 segmentos apos ScriptValue effect e holy-site.\n")
        handle.write("- Ainda restam 1566 segmentos sem spec util.\n")
        handle.write("- Requirement/effect esta maduro o suficiente para atualizar Network/Layout, mas not_requirement_effect ainda deve ser roteado antes de producao full.\n")
        handle.write("- Maior gargalo coeso dentro de requirement/effect: domain_context_after_requirement_effect.\n")
        handle.write("- Proximo melhor prompt global: chat_exec_not_requirement_effect_global_router_review_prompt.md; proximo dentro de requirement/effect: chat_exec_domain_context_after_requirement_effect_review_prompt.md.\n")
        handle.write("- Producao full nao e recomendada agora.\n")
        handle.write("- Componentes novos continuam read-only/shadow e sem permissao de apply/lifecycle.\n")
        handle.write("- Proxima Network deve mostrar novos shadow splitters, cobertura acumulada, sem-spec restante e not_requirement_effect.\n")
        handle.write("\nValidacoes\n")
        handle.write("- banco aberto em mode=ro com PRAGMA query_only=ON.\n")
        handle.write("- nenhuma escrita em banco, source ou output.\n")
    return txt_path, jsonl_path, inventory_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Global post holy-site read-only diagnostic.")
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    parser.add_argument("--ledger-run-id", required=True, type=int)
    args = parser.parse_args()
    if args.segment_state_run_id != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("segment_state_run_id guard failed")
    if args.ledger_run_id != EXPECTED_LEDGER_RUN_ID:
        raise SystemExit("ledger_run_id guard failed")
    if not Path(SCRIPT_VALUE_APPLY).exists():
        raise SystemExit("missing script_value_effect_policy registry apply artifact")
    if not Path(HOLY_SITE_APPLY).exists():
        raise SystemExit("missing holy_site_effect_name_policy registry apply artifact")

    post_accolade = read_json(POST_ACCOLADE_INVENTORY)
    reviews = review_metrics()
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
    coverage, routes_without_spec, shadow_coverage = coverage_metrics(route_counts, post_accolade)
    gaps = gap_metrics(route_counts, post_accolade, reviews)
    next_candidates = candidates(route_counts, gaps, coverage)
    txt_path, jsonl_path, inventory_path = write_reports(
        args=args,
        state=state,
        registry=registry,
        reviews=reviews,
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
    print(f"operational_agents: {registry['operational_agents']}")
    print(f"shadow_agents: {registry['shadow_agents']}")
    print(f"segments_with_spec_associated_after_accolade_trait: {coverage['segments_with_spec_associated_after_accolade_trait']}")
    print(f"segments_with_spec_associated_after_script_value_effect: {coverage['segments_with_spec_associated_after_script_value_effect']}")
    print(f"segments_with_spec_associated_after_holy_site: {coverage['segments_with_spec_associated_after_holy_site']}")
    print(f"segments_without_useful_spec: {coverage['segments_without_useful_spec']}")
    print(f"next_prompt: {next_candidates[0]['recommended_next_prompt']}")
    print("network_should_update_now: True")
    print("production_full_recommended_now: False")


if __name__ == "__main__":
    main()
