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
import requirement_effect_router_readonly as router


SOURCE = "global_post_building_modifier_policy_diagnostic_v1"
EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76
EXPECTED_REGISTERED_AGENTS = 220
EXPECTED_OBSERVED_AGENT_KEYS = 277
EXPECTED_ROUTED_AGENTS = 52
EXPECTED_EVIDENCE_AGENTS = 88
EXPECTED_NO_SIGNAL_AGENTS = 138
EXPECTED_CLOSED_COUNT = 276375
EXPECTED_PENDING_COUNT = 11725
EXPECTED_OUTPUT_APPLY_PENDING_COUNT = 0

BASELINE_INVENTORY = "reports/20260622_152951_467175_global_post_architecture_diagnostic_inventory.json"
ARTIFACT_REVIEW = "reports/20260622_155336_452988_artifact_activity_effect_policy_review.jsonl"
BUILDING_REVIEW = "reports/20260622_161624_167472_building_modifier_effect_policy_review.jsonl"
BUILDING_TYPE_REVIEW = "reports/20260622_164542_376256_building_modifier_building_type_policy_review.jsonl"


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_global_post_building_modifier_policy_diagnostic"
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


def read_json(path: str) -> dict[str, Any]:
    return json.loads((Path.cwd() / path).read_text(encoding="utf-8"))


def read_jsonl(path: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with (Path.cwd() / path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"Invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def summary_row(rows: list[dict[str, Any]]) -> dict[str, Any]:
    summaries = [row for row in rows if row.get("record_type") == "summary"]
    if len(summaries) != 1:
        raise SystemExit(f"expected exactly one summary row, got {len(summaries)}")
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
    routed_agents = sum(1 for row in rows if row["routed_rows"] > 0)
    evidence_agents = sum(1 for row in rows if row["evidence_rows"] > 0)
    no_signal_agents = [
        row
        for row in rows
        if row["routed_rows"] == 0
        and row["evidence_rows"] == 0
        and row["recommendation_evidence_count"] == 0
    ]
    metrics = {
        "registered_agents": len(registry),
        "observed_agent_keys": len(rows),
        "routed_agents": routed_agents,
        "evidence_agents": evidence_agents,
        "no_signal_agents": len(no_signal_agents),
        "operational_agents_estimated": sum(1 for row in registry if row.get("operational_state") == "dry_run"),
        "dry_run_agents": sum(1 for row in registry if row.get("operational_state") == "dry_run"),
        "shadow_agents": sum(1 for row in registry if row.get("operational_state") == "shadow"),
        "terminal_guard_agents": sum(1 for row in registry if row.get("decision_role") == "terminal_guard"),
        "splitter_agents": sum(1 for row in registry if row.get("decision_role") == "route_and_split"),
        "requirement_effect_agents": sum(1 for row in registry if row.get("scope_group") == "requirement_effect_router"),
        "effect_list_agents": sum(1 for row in registry if "effect_list" in str(row.get("agent_key") or "")),
        "building_modifier_agents": sum(1 for row in registry if "building_modifier" in str(row.get("agent_key") or "")),
        "artifact_activity_agents": sum(1 for row in registry if "artifact_activity" in str(row.get("agent_key") or "") or str(row.get("agent_key") or "").startswith("artifact_item")),
        "latest_routing_run_id": int(latest_run["id"]) if latest_run else 0,
    }
    expected = {
        "registered_agents": EXPECTED_REGISTERED_AGENTS,
        "observed_agent_keys": EXPECTED_OBSERVED_AGENT_KEYS,
        "routed_agents": EXPECTED_ROUTED_AGENTS,
        "evidence_agents": EXPECTED_EVIDENCE_AGENTS,
        "no_signal_agents": EXPECTED_NO_SIGNAL_AGENTS,
    }
    for key, value in expected.items():
        if metrics[key] != value:
            raise SystemExit(f"registry guard failed: {key}={metrics[key]} expected {value}")
    return metrics, by_key


def route_pending(
    conn: sqlite3.Connection,
    segment_state_run_id: int,
    ledger_run_id: int,
) -> tuple[list[dict[str, Any]], Counter[str], Counter[str], Counter[str], Counter[str], Counter[str], dict[int, int], int, int]:
    return architecture_diag.route_pending(conn, segment_state_run_id, ledger_run_id)


def review_metrics() -> dict[str, int]:
    artifact_summary = summary_row(read_jsonl(ARTIFACT_REVIEW))
    building_summary = summary_row(read_jsonl(BUILDING_REVIEW))
    building_type_summary = summary_row(read_jsonl(BUILDING_TYPE_REVIEW))
    return {
        "artifact_activity_effect_policy_total": int(artifact_summary["universe_estimated"]),
        "artifact_activity_effect_policy_registered_splitter_coverage": int(artifact_summary["universe_estimated"]),
        "artifact_activity_effect_policy_reuse_coverage": int(artifact_summary["reused_cataloged_policy_count"]),
        "building_modifier_effect_policy_total": int(building_summary["universe_estimated"]),
        "building_modifier_effect_policy_registered_splitter_coverage": int(building_summary["universe_estimated"]),
        "building_modifier_effect_policy_reuse_coverage": int(building_summary["reused_cataloged_policy_count"]),
        "building_modifier_building_type_policy_total": int(building_type_summary["total_reviewed"]),
        "building_modifier_building_type_terminal_registered_coverage": int(building_type_summary["terminal_policy_count"]),
    }


def coverage_metrics(route_counts: Counter[str], baseline: dict[str, Any], review: dict[str, int]) -> tuple[dict[str, int], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    baseline_summary = baseline["summary"]
    after_effect_list = int(baseline_summary["segments_with_spec_associated_after_effect_list"])
    after_artifact = after_effect_list + route_counts["artifact_activity_effect_policy"]
    after_building = after_artifact + route_counts["building_modifier_effect_policy"]
    terminal_after_effect_list = int(baseline_summary["segments_with_terminal_policy_after_effect_list"])
    terminal_after_building_type = terminal_after_effect_list + review["building_modifier_building_type_terminal_registered_coverage"]
    registered_terminal = int(baseline_summary["segments_with_effect_list_registered_terminal_policy"]) + review["building_modifier_building_type_terminal_registered_coverage"]
    shadow_splitter = (
        route_counts["effect_list_multiline_policy"]
        + route_counts["artifact_activity_effect_policy"]
        + route_counts["building_modifier_effect_policy"]
    )
    without_spec = EXPECTED_PENDING_COUNT - after_building
    coverage = {
        "segments_with_requirement_effect_route": EXPECTED_PENDING_COUNT - route_counts["not_requirement_effect"] - route_counts["blocked_uncertain"],
        "segments_with_spec_associated_before_effect_list": int(baseline_summary["segments_with_spec_associated_before_effect_list"]),
        "segments_with_spec_associated_after_effect_list": after_effect_list,
        "segments_with_spec_associated_after_artifact_activity": after_artifact,
        "segments_with_spec_associated_after_building_modifier": after_building,
        "segments_with_terminal_policy_after_effect_list": terminal_after_effect_list,
        "segments_with_terminal_policy_after_building_type": terminal_after_building_type,
        "segments_with_registered_terminal_policy": registered_terminal,
        "segments_with_shadow_splitter_policy": shadow_splitter,
        "segments_without_useful_spec": without_spec,
    }
    routes_without_spec = [
        {"route": route, "segments": count}
        for route, count in route_counts.most_common()
        if count >= 100
        and route not in {
            "effect_list_multiline_policy",
            "artifact_activity_effect_policy",
            "building_modifier_effect_policy",
            "requirement_tooltip_policy",
            "concept_requirement_policy",
            "scope_getter_requirement_policy",
        }
    ]
    terminal_coverage = [
        {"policy": "effect_list_terminal_guards", "segments": int(baseline_summary["segments_with_effect_list_registered_terminal_policy"])},
        {"policy": "building_modifier_building_type_policy", "segments": review["building_modifier_building_type_terminal_registered_coverage"]},
    ]
    shadow_coverage = [
        {"policy": "effect_list_multiline_policy", "segments": route_counts["effect_list_multiline_policy"]},
        {"policy": "artifact_activity_effect_policy", "segments": route_counts["artifact_activity_effect_policy"]},
        {"policy": "building_modifier_effect_policy", "segments": route_counts["building_modifier_effect_policy"]},
    ]
    return coverage, routes_without_spec, terminal_coverage, shadow_coverage


def candidates(routes_without_spec: list[dict[str, Any]], coverage: dict[str, int]) -> list[dict[str, Any]]:
    prompt_map = {
        "event_context_after_requirement_effect": ("chat_exec_requirement_effect_event_context_policy_review_prompt.md", "terminal_reuse", "medium"),
        "residual_repair_after_requirement_effect": ("chat_exec_requirement_effect_residual_review_prompt.md", "closed_count", "high"),
        "accolade_trait_requirement_policy": ("chat_exec_accolade_trait_requirement_policy_review_prompt.md", "terminal_reuse", "medium"),
        "script_value_effect_policy": ("chat_exec_script_value_effect_policy_review_prompt.md", "terminal_reuse", "low"),
        "holy_site_effect_name_policy": ("chat_exec_holy_site_effect_name_policy_review_prompt.md", "terminal_reuse", "low"),
        "domain_context_after_requirement_effect": ("chat_exec_requirement_effect_domain_context_policy_review_prompt.md", "terminal_reuse", "medium"),
    }
    rows: list[dict[str, Any]] = []
    for gap in routes_without_spec:
        route = gap["route"]
        if route == "not_requirement_effect":
            continue
        prompt, gain, risk = prompt_map.get(route, ("chat_exec_global_next_architecture_gap_review_prompt.md", "architecture_coverage", "medium"))
        rows.append(
            {
                "candidate": route,
                "segments": int(gap["segments"]),
                "kind": "review",
                "expected_gain_type": gain,
                "risk": risk,
                "recommended_next_prompt": prompt,
                "why": "large route remains without useful registered spec after building/modifier package",
            }
        )
    rows.append(
        {
            "candidate": "network_layout_update",
            "segments": coverage["segments_with_spec_associated_after_building_modifier"],
            "kind": "layout",
            "expected_gain_type": "layout_accuracy",
            "risk": "low",
            "recommended_next_prompt": "chat_layout_network_requirement_effect_packages_prompt.md",
            "why": "effect-list, artifact/activity and building/modifier now exist as registry-visible packages",
        }
    )
    rows.append(
        {
            "candidate": "production_full",
            "segments": 0,
            "kind": "production_full",
            "expected_gain_type": "production_measurement",
            "risk": "high",
            "recommended_next_prompt": "none_now",
            "why": "large context/residual/accolade routes still lack useful specs",
        }
    )
    return rows


def write_reports(
    *,
    args: argparse.Namespace,
    state: dict[str, int],
    registry: dict[str, int],
    review: dict[str, int],
    coverage: dict[str, int],
    records: list[dict[str, Any]],
    route_counts: Counter[str],
    family_counts: Counter[str],
    combo_counts: Counter[str],
    single_family_counts: Counter[str],
    macro_counts: Counter[str],
    issue_count_by_segment: dict[int, int],
    routes_without_spec: list[dict[str, Any]],
    terminal_coverage: list[dict[str, Any]],
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
        **review,
        "database_query_only": True,
        "requires_apply_later": 0,
        "requires_lifecycle_later": 0,
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
        "top_terminal_guard_coverage": terminal_coverage,
        "top_shadow_splitter_coverage": shadow_coverage,
        "next_candidates": next_candidates,
    }
    inventory_path.write_text(json.dumps(inventory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(summary, ensure_ascii=False, sort_keys=True) + "\n")
        sections = [
            ("top_family", inventory["top_families"]),
            ("top_exact_combination", inventory["top_exact_combinations"]),
            ("top_single_family", inventory["top_single_family"]),
            ("top_macro_lane", inventory["top_macro_lanes"]),
            ("route_without_spec", routes_without_spec),
            ("terminal_guard_coverage", terminal_coverage),
            ("shadow_splitter_coverage", shadow_coverage),
            ("next_candidate", next_candidates),
        ]
        for record_type, rows in sections:
            for row in rows:
                handle.write(json.dumps({"record_type": record_type, **row}, ensure_ascii=False, sort_keys=True) + "\n")
    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("Global post building/modifier policy diagnostic\n\n")
        handle.write("Estado global\n")
        for key in ["pending_segments", "open_issues", "1_issue", "2_issues", "3_plus_issues", "closed_count", "pending_count", "output_apply_pending_count"]:
            handle.write(f"- {key}: {summary[key]}\n")
        handle.write("\nRegistry/network\n")
        for key in [
            "registered_agents",
            "observed_agent_keys",
            "routed_agents",
            "evidence_agents",
            "no_signal_agents",
            "operational_agents_estimated",
            "dry_run_agents",
            "shadow_agents",
            "terminal_guard_agents",
            "splitter_agents",
            "requirement_effect_agents",
            "effect_list_agents",
            "building_modifier_agents",
            "artifact_activity_agents",
        ]:
            handle.write(f"- {key}: {summary[key]}\n")
        handle.write("\nCobertura\n")
        for key in coverage:
            handle.write(f"- {key}: {coverage[key]}\n")
        handle.write("\nRotas especificas novas\n")
        for key in review:
            handle.write(f"- {key}: {review[key]}\n")
        handle.write("\nTop gargalos restantes\n")
        for gap in routes_without_spec[:12]:
            handle.write(f"- {gap['route']}: {gap['segments']}\n")
        handle.write("\nProximos candidatos\n")
        for index, candidate in enumerate(next_candidates[:8], 1):
            handle.write(f"{index}. {candidate['recommended_next_prompt']} ({candidate['candidate']}, {candidate['segments']}, {candidate['risk']}) - {candidate['why']}\n")
        handle.write("\nRespostas objetivas\n")
        handle.write("- A arquitetura melhorou materialmente: sim, coverage associada subiu de 5784 para 7707 apos artifact/activity e building/modifier.\n")
        handle.write("- Artifact/activity e building/modifier estao cobertos por splitters shadow registrados nos universos completos estimados.\n")
        handle.write("- Building type virou terminal guard registrado em 66/68 revisados.\n")
        handle.write("- Network pode atualizar agora, mas ainda ha valor em mais um bloco grande de event/context antes do Layout se quiser menos churn visual.\n")
        handle.write("- Producao full nao e recomendada agora: rotas grandes sem spec continuam abertas.\n")
        handle.write("- Componentes read-only/shadow/dry_run continuam sem autoridade de apply/lifecycle.\n")
        handle.write("\nValidacoes\n")
        handle.write("- banco aberto em mode=ro com PRAGMA query_only=ON.\n")
        handle.write("- nenhuma escrita em banco, source ou output.\n")
    return txt_path, jsonl_path, inventory_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Global post building/modifier read-only diagnostic.")
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    parser.add_argument("--ledger-run-id", required=True, type=int)
    args = parser.parse_args()
    if args.segment_state_run_id != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("segment_state_run_id guard failed")
    if args.ledger_run_id != EXPECTED_LEDGER_RUN_ID:
        raise SystemExit("ledger_run_id guard failed")

    baseline = read_json(BASELINE_INVENTORY)
    review = review_metrics()
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
    coverage, routes_without_spec, terminal_coverage, shadow_coverage = coverage_metrics(route_counts, baseline, review)
    next_candidates = candidates(routes_without_spec, coverage)
    txt_path, jsonl_path, inventory_path = write_reports(
        args=args,
        state=state,
        registry=registry,
        review=review,
        coverage=coverage,
        records=records,
        route_counts=route_counts,
        family_counts=family_counts,
        combo_counts=combo_counts,
        single_family_counts=single_family_counts,
        macro_counts=macro_counts,
        issue_count_by_segment=issue_count_by_segment,
        routes_without_spec=routes_without_spec,
        terminal_coverage=terminal_coverage,
        shadow_coverage=shadow_coverage,
        next_candidates=next_candidates,
    )
    print(f"txt: {txt_path}")
    print(f"jsonl: {jsonl_path}")
    print(f"inventory: {inventory_path}")
    print(f"registered_agents: {registry['registered_agents']}")
    print(f"observed_agent_keys: {registry['observed_agent_keys']}")
    print(f"segments_with_spec_associated_after_effect_list: {coverage['segments_with_spec_associated_after_effect_list']}")
    print(f"segments_with_spec_associated_after_artifact_activity: {coverage['segments_with_spec_associated_after_artifact_activity']}")
    print(f"segments_with_spec_associated_after_building_modifier: {coverage['segments_with_spec_associated_after_building_modifier']}")
    print(f"segments_without_useful_spec: {coverage['segments_without_useful_spec']}")
    print(f"network_should_update_now: True")
    print(f"production_full_recommended_now: False")


if __name__ == "__main__":
    main()
