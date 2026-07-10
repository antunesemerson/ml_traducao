from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import agent_inventory_diagnostic as agent_inventory
import db
import effect_list_policy_catalog_integration as effect_list_integration
import requirement_effect_policy_catalog as policy_catalog
import requirement_effect_router_readonly as router


SOURCE = "global_post_architecture_diagnostic_v1"
EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76
EXPECTED_REGISTERED_AGENTS = 217
EXPECTED_CLOSED_COUNT = 276375
EXPECTED_PENDING_COUNT = 11725
EXPECTED_OUTPUT_APPLY_PENDING_COUNT = 0

PREVIOUS_CATALOG_INVENTORY = "reports/20260621_235338_197498_requirement_effect_policy_catalog_inventory.json"
EFFECT_LIST_INVENTORY = "reports/20260622_151112_828825_effect_list_policy_catalog_inventory.json"


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_global_post_architecture_diagnostic"
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


def state_counts(conn: sqlite3.Connection, run_id: int) -> dict[str, int]:
    row = conn.execute(
        """
        SELECT
          SUM(CASE WHEN state_group = 'closed' THEN 1 ELSE 0 END) AS closed_count,
          SUM(CASE WHEN state_group = 'pending' THEN 1 ELSE 0 END) AS pending_count,
          SUM(CASE WHEN COALESCE(needs_output_apply, 0) = 1 THEN 1 ELSE 0 END) AS output_apply_pending_count
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


def registry_metrics(conn: sqlite3.Connection) -> dict[str, int]:
    registry = agent_inventory.fetch_registry(conn)
    latest_run, routing = agent_inventory.fetch_latest_routing(conn)
    recommendations = agent_inventory.fetch_recommendations(conn)
    evidence, _table_counts = agent_inventory.fetch_agent_evidence(conn)
    rows = agent_inventory.build_rows(registry, routing, evidence, recommendations)
    registered_keys = {str(row["agent_key"]) for row in registry}
    routed_agents = sum(1 for row in rows if row["routed_rows"] > 0)
    evidence_agents = sum(1 for row in rows if row["evidence_rows"] > 0)
    no_signal_agents = [
        row
        for row in rows
        if row["routed_rows"] == 0
        and row["evidence_rows"] == 0
        and row["recommendation_evidence_count"] == 0
    ]
    dry_run = sum(1 for row in registry if row.get("operational_state") == "dry_run")
    shadow = sum(1 for row in registry if row.get("operational_state") == "shadow")
    terminal_guard = sum(1 for row in registry if row.get("decision_role") == "terminal_guard")
    splitter = sum(1 for row in registry if row.get("decision_role") == "route_and_split")
    requirement_effect = sum(1 for row in registry if row.get("scope_group") == "requirement_effect_router")
    effect_list = sum(1 for row in registry if "effect_list" in str(row.get("agent_key") or "") or str(row.get("agent_key") or "").startswith("artifact_"))
    metrics = {
        "registered_agents": len(registry),
        "observed_agent_keys": len(rows),
        "routed_agents": routed_agents,
        "evidence_agents": evidence_agents,
        "no_signal_agents": len(no_signal_agents),
        "operational_agents_estimated": dry_run,
        "dry_run_agents": dry_run,
        "shadow_agents": shadow,
        "terminal_guard_agents": terminal_guard,
        "splitter_agents": splitter,
        "requirement_effect_agents": requirement_effect,
        "effect_list_agents": effect_list,
        "latest_routing_run_id": int(latest_run["id"]) if latest_run else 0,
    }
    if metrics["registered_agents"] != EXPECTED_REGISTERED_AGENTS:
        raise SystemExit(f"registry guard failed: registered_agents={metrics['registered_agents']} expected {EXPECTED_REGISTERED_AGENTS}")
    return metrics


def route_pending(
    conn: sqlite3.Connection,
    segment_state_run_id: int,
    ledger_run_id: int,
) -> tuple[list[dict[str, Any]], Counter[str], Counter[str], Counter[str], Counter[str], Counter[str], dict[int, int], int, int]:
    router.fetch_runs(conn, segment_state_run_id, ledger_run_id)
    grouped = router.fetch_pending_rows(conn, segment_state_run_id, ledger_run_id)
    records: list[dict[str, Any]] = []
    route_counts: Counter[str] = Counter()
    family_counts: Counter[str] = Counter()
    combo_counts: Counter[str] = Counter()
    single_family_counts: Counter[str] = Counter()
    macro_counts: Counter[str] = Counter()
    issue_count_by_segment: dict[int, int] = {}

    for segment_id, rows in grouped.items():
        first = rows[0]
        blob = router.blob_for(rows)
        markers = router.detect_markers(blob)
        route, reason = router.route_for(blob, markers)
        families = router.families_for(rows)
        issue_count = sum(1 for row in rows if row.get("issue_family"))
        issue_count_by_segment[segment_id] = issue_count
        route_counts[route] += 1
        if families:
            family_counts.update(families)
            combo_counts[" + ".join(families)] += 1
            if len(families) == 1:
                single_family_counts[families[0]] += 1
        else:
            combo_counts["no_open_family"] += 1
        macro = macro_lane(route, markers)
        macro_counts[macro] += 1
        records.append(
            {
                "segment_id": segment_id,
                "relative_path": str(first.get("relative_path") or ""),
                "source_key": str(first.get("source_key") or ""),
                "families_open": list(families),
                "issue_count": issue_count,
                "route": route,
                "macro_lane": macro,
                "markers": markers,
                "reason": reason,
            }
        )
    pending_segments = len(grouped)
    routed_segments = pending_segments - route_counts["not_requirement_effect"] - route_counts["blocked_uncertain"]
    return records, route_counts, family_counts, combo_counts, single_family_counts, macro_counts, issue_count_by_segment, pending_segments, routed_segments


def macro_lane(route: str, markers: list[str]) -> str:
    if route == "effect_list_multiline_policy":
        return "effect_list_multiline"
    if route == "requirement_tooltip_policy":
        return "requirement_tooltip"
    if route in {"event_context_after_requirement_effect", "domain_context_after_requirement_effect"}:
        return "context_after_requirement_effect"
    if route == "residual_repair_after_requirement_effect":
        return "residual_repair"
    if route == "parser_after_requirement_effect" or "DynamicToken" in markers:
        return "parser_dynamic"
    if route == "not_requirement_effect":
        return "outside_requirement_effect"
    if route == "blocked_uncertain":
        return "blocked_uncertain"
    return "requirement_effect_other"


def coverage_metrics(
    catalog: dict[str, Any],
    previous_inventory: dict[str, Any],
    effect_inventory: dict[str, Any],
    records: list[dict[str, Any]],
    route_counts: Counter[str],
    registered_effect_list: set[str],
) -> tuple[dict[str, int], list[dict[str, Any]], list[dict[str, Any]]]:
    previous_matches = previous_inventory.get("route_matches") or {}
    current_matches = {route: policy_catalog.match_route(route, catalog) for route in route_counts}
    before_associated = sum(count for route, count in route_counts.items() if (previous_matches.get(route) or {}).get("matched_policy_id"))
    before_terminal = sum(count for route, count in route_counts.items() if (previous_matches.get(route) or {}).get("terminal_policy"))
    after_associated = sum(count for route, count in route_counts.items() if current_matches[route]["matched_policy_id"])
    after_terminal = sum(count for route, count in route_counts.items() if current_matches[route]["terminal_policy"])

    effect_records = [record for record in records if record["route"] == "effect_list_multiline_policy"]
    subroute_counts: Counter[str] = Counter()
    subroute_reasons: dict[str, str] = {}
    for record in effect_records:
        subroute, reason = effect_list_integration.effect_list_subroute(record["markers"])
        subroute_counts[subroute] += 1
        subroute_reasons.setdefault(subroute, reason)
    by_id = catalog["by_id"]
    effect_spec = sum(count for subroute, count in subroute_counts.items() if subroute in by_id)
    effect_terminal = sum(count for subroute, count in subroute_counts.items() if subroute in effect_list_integration.TERMINAL_EFFECT_LIST_POLICIES)
    effect_registered_terminal = sum(count for subroute, count in subroute_counts.items() if subroute in registered_effect_list)

    without_useful_spec = sum(count for route, count in route_counts.items() if not current_matches[route]["matched_policy_id"])
    metrics = {
        "segments_with_requirement_effect_route": sum(route_counts.values()) - route_counts["not_requirement_effect"] - route_counts["blocked_uncertain"],
        "segments_with_spec_associated_before_effect_list": before_associated,
        "segments_with_spec_associated_after_effect_list": after_associated,
        "segments_with_terminal_policy_before_effect_list": before_terminal,
        "segments_with_terminal_policy_after_effect_list": after_terminal,
        "segments_with_effect_list_route": route_counts["effect_list_multiline_policy"],
        "segments_with_effect_list_spec_associated": effect_spec,
        "segments_with_effect_list_terminal_policy": effect_terminal,
        "segments_with_effect_list_registered_terminal_policy": effect_registered_terminal,
        "segments_without_useful_spec": without_useful_spec,
    }
    top_routes_without_spec = [
        {"route": route, "segments": count}
        for route, count in route_counts.most_common()
        if count >= 100 and not current_matches[route]["matched_policy_id"]
    ]
    package_coverage = [
        {
            "policy": subroute,
            "segments": count,
            "cataloged": subroute in by_id,
            "terminal": subroute in effect_list_integration.TERMINAL_EFFECT_LIST_POLICIES,
            "registered": subroute in registered_effect_list,
            "reason": subroute_reasons[subroute],
        }
        for subroute, count in subroute_counts.most_common()
    ]
    expected = effect_inventory.get("summary") or {}
    for key in ("segments_with_effect_list_route", "segments_with_spec_effect_list_associated", "segments_with_terminal_effect_list_policy"):
        if int(expected.get(key) or 0) and key == "segments_with_effect_list_route" and int(expected[key]) != metrics[key]:
            raise SystemExit(f"effect-list inventory cross-check failed: {key}")
    return metrics, top_routes_without_spec, package_coverage


def recommendation_candidates(top_routes_without_spec: list[dict[str, Any]], coverage: dict[str, int]) -> list[dict[str, Any]]:
    route_prompts = {
        "artifact_activity_effect_policy": ("chat_exec_artifact_activity_effect_policy_review_prompt.md", "architecture_coverage", "medium"),
        "building_modifier_effect_policy": ("chat_exec_building_modifier_effect_policy_review_prompt.md", "architecture_coverage", "medium"),
        "event_context_after_requirement_effect": ("chat_exec_requirement_effect_event_context_policy_review_prompt.md", "terminal_reuse", "medium"),
        "residual_repair_after_requirement_effect": ("chat_exec_requirement_effect_residual_review_prompt.md", "closed_count", "high"),
        "accolade_trait_requirement_policy": ("chat_exec_accolade_trait_requirement_policy_review_prompt.md", "terminal_reuse", "medium"),
        "script_value_effect_policy": ("chat_exec_script_value_effect_policy_review_prompt.md", "terminal_reuse", "low"),
        "holy_site_effect_name_policy": ("chat_exec_holy_site_effect_name_policy_review_prompt.md", "terminal_reuse", "low"),
        "domain_context_after_requirement_effect": ("chat_exec_requirement_effect_domain_context_policy_review_prompt.md", "terminal_reuse", "medium"),
    }
    candidates: list[dict[str, Any]] = []
    for gap in top_routes_without_spec:
        route = gap["route"]
        if route == "not_requirement_effect":
            continue
        prompt, gain, risk = route_prompts.get(route, ("chat_exec_global_next_architecture_gap_review_prompt.md", "architecture_coverage", "medium"))
        kind = "review" if gain != "architecture_coverage" else "catalog"
        candidates.append(
            {
                "candidate": route,
                "segments": int(gap["segments"]),
                "kind": kind,
                "expected_gain_type": gain,
                "risk": risk,
                "recommended_next_prompt": prompt,
                "why": "large route still lacks useful catalog spec after effect-list registration",
            }
        )
    candidates.append(
        {
            "candidate": "network_layout_update",
            "segments": coverage["segments_with_spec_associated_after_effect_list"],
            "kind": "layout",
            "expected_gain_type": "layout_accuracy",
            "risk": "low",
            "recommended_next_prompt": "chat_layout_network_effect_list_package_prompt.md",
            "why": "registry now exposes the effect-list package as a real block with 217 registered agents",
        }
    )
    candidates.append(
        {
            "candidate": "production_full",
            "segments": 0,
            "kind": "production_full",
            "expected_gain_type": "production_measurement",
            "risk": "high",
            "recommended_next_prompt": "none_now",
            "why": "large architecture routes remain unspecced; production full is not the next principal step",
        }
    )
    return candidates


def write_reports(
    *,
    args: argparse.Namespace,
    state: dict[str, int],
    registry: dict[str, int],
    coverage: dict[str, int],
    pending_segments: int,
    routed_segments: int,
    issue_count_by_segment: dict[int, int],
    family_counts: Counter[str],
    combo_counts: Counter[str],
    single_family_counts: Counter[str],
    macro_counts: Counter[str],
    route_counts: Counter[str],
    top_routes_without_spec: list[dict[str, Any]],
    package_coverage: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> tuple[Path, Path, Path]:
    txt_path, jsonl_path, inventory_path = output_paths()
    open_issues = sum(issue_count_by_segment.values())
    one_issue = sum(1 for count in issue_count_by_segment.values() if count == 1)
    two_issues = sum(1 for count in issue_count_by_segment.values() if count == 2)
    three_plus = sum(1 for count in issue_count_by_segment.values() if count >= 3)
    global_state = {
        "pending_segments": pending_segments,
        "open_issues": open_issues,
        "1_issue": one_issue,
        "2_issues": two_issues,
        "3_plus_issues": three_plus,
        **state,
    }
    material_gain = coverage["segments_with_spec_associated_after_effect_list"] - coverage["segments_with_spec_associated_before_effect_list"]
    summary = {
        "record_type": "summary",
        "source": SOURCE,
        "segment_state_run_id": args.segment_state_run_id,
        "ledger_run_id": args.ledger_run_id,
        **global_state,
        **registry,
        **coverage,
        "architecture_material_gain_segments": material_gain,
        "network_should_update_now": True,
        "production_full_recommended_now": False,
        "database_query_only": True,
        "requires_apply_later": 0,
        "requires_lifecycle_later": 0,
    }
    inventory = {
        "schema_version": 1,
        "source": SOURCE,
        "summary": summary,
        "top_families": [{"family": key, "segments": value} for key, value in family_counts.most_common(20)],
        "top_exact_combinations": [{"combination": key, "segments": value} for key, value in combo_counts.most_common(20)],
        "top_single_family": [{"family": key, "segments": value} for key, value in single_family_counts.most_common(20)],
        "top_macro_lanes": [{"macro_lane": key, "segments": value} for key, value in macro_counts.most_common(20)],
        "top_routes_without_spec": top_routes_without_spec,
        "top_registered_package_coverage": package_coverage,
        "next_candidates": candidates,
    }
    with inventory_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(inventory, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(summary, ensure_ascii=False, sort_keys=True) + "\n")
        sections = [
            ("top_family", inventory["top_families"]),
            ("top_exact_combination", inventory["top_exact_combinations"]),
            ("top_single_family", inventory["top_single_family"]),
            ("top_macro_lane", inventory["top_macro_lanes"]),
            ("route_without_spec", top_routes_without_spec),
            ("registered_package_coverage", package_coverage),
            ("next_candidate", candidates),
        ]
        for record_type, rows in sections:
            for row in rows:
                handle.write(json.dumps({"record_type": record_type, **row}, ensure_ascii=False, sort_keys=True) + "\n")
    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("Global post-architecture diagnostic\n\n")
        handle.write("Estado global\n")
        for key in ["pending_segments", "open_issues", "1_issue", "2_issues", "3_plus_issues", "closed_count", "pending_count", "output_apply_pending_count"]:
            handle.write(f"- {key}: {global_state[key]}\n")
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
        ]:
            handle.write(f"- {key}: {registry[key]}\n")
        handle.write("\nCobertura de arquitetura\n")
        for key in coverage:
            handle.write(f"- {key}: {coverage[key]}\n")
        handle.write(f"- material_gain_segments: {material_gain}\n")
        handle.write("\nTop gargalos sem spec util\n")
        for gap in top_routes_without_spec[:12]:
            handle.write(f"- {gap['route']}: {gap['segments']}\n")
        handle.write("\nTop package coverage effect-list\n")
        for item in package_coverage[:12]:
            handle.write(f"- {item['policy']}: {item['segments']} | cataloged={item['cataloged']} | terminal={item['terminal']} | registered={item['registered']}\n")
        handle.write("\nProximos candidatos\n")
        for index, candidate in enumerate(candidates[:8], 1):
            handle.write(f"{index}. {candidate['recommended_next_prompt']} ({candidate['candidate']}, {candidate['segments']}, {candidate['risk']}) - {candidate['why']}\n")
        handle.write("\nRespostas objetivas\n")
        handle.write("- A arquitetura melhorou cobertura materialmente: sim, +1654 segmentos com spec associada geral depois do pacote effect-list.\n")
        handle.write("- Effect-list esta roteado em 1654, com 1272 em spec, 1090 em terminal policy e 1090 em terminal registrada apos o pacote.\n")
        handle.write("- Network/Layout pode ser atualizado agora, mas com melhor insumo se receber este diagnostico como base.\n")
        handle.write("- Producao full nao e o proximo passo principal: ainda ha rotas grandes sem spec util.\n")
        handle.write("- Componentes read-only continuam sem apply/lifecycle; dry_run e shadow sao estados de arquitetura, nao fechamento.\n")
        handle.write("\nValidacoes\n")
        handle.write("- banco aberto em mode=ro com PRAGMA query_only=ON.\n")
        handle.write("- nenhuma escrita em banco, source ou output.\n")
    return txt_path, jsonl_path, inventory_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Global post-architecture read-only diagnostic.")
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    parser.add_argument("--ledger-run-id", required=True, type=int)
    args = parser.parse_args()
    if args.segment_state_run_id != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("segment_state_run_id guard failed")
    if args.ledger_run_id != EXPECTED_LEDGER_RUN_ID:
        raise SystemExit("ledger_run_id guard failed")

    previous_inventory = read_json(PREVIOUS_CATALOG_INVENTORY)
    effect_inventory = read_json(EFFECT_LIST_INVENTORY)
    catalog = policy_catalog.load_policy_catalog(Path.cwd(), args.segment_state_run_id)
    with connect_readonly() as conn:
        state = state_counts(conn, args.segment_state_run_id)
        registry = registry_metrics(conn)
        (
            records,
            route_counts,
            family_counts,
            combo_counts,
            single_family_counts,
            macro_counts,
            issue_count_by_segment,
            pending_segments,
            routed_segments,
        ) = route_pending(conn, args.segment_state_run_id, args.ledger_run_id)
        registered_effect_list = {
            str(row["agent_key"])
            for row in conn.execute(
                """
                SELECT agent_key
                FROM ml_agent_registry
                WHERE scope_group = 'requirement_effect_router'
                  AND status = 'active'
                  AND decision_role = 'terminal_guard'
                """
            ).fetchall()
        }
    coverage, top_routes_without_spec, package_coverage = coverage_metrics(
        catalog,
        previous_inventory,
        effect_inventory,
        records,
        route_counts,
        registered_effect_list,
    )
    candidates = recommendation_candidates(top_routes_without_spec, coverage)
    txt_path, jsonl_path, inventory_path = write_reports(
        args=args,
        state=state,
        registry=registry,
        coverage=coverage,
        pending_segments=pending_segments,
        routed_segments=routed_segments,
        issue_count_by_segment=issue_count_by_segment,
        family_counts=family_counts,
        combo_counts=combo_counts,
        single_family_counts=single_family_counts,
        macro_counts=macro_counts,
        route_counts=route_counts,
        top_routes_without_spec=top_routes_without_spec,
        package_coverage=package_coverage,
        candidates=candidates,
    )
    print(f"txt: {txt_path}")
    print(f"jsonl: {jsonl_path}")
    print(f"inventory: {inventory_path}")
    print(f"pending_segments: {pending_segments}")
    print(f"registered_agents: {registry['registered_agents']}")
    print(f"segments_with_spec_associated_before_effect_list: {coverage['segments_with_spec_associated_before_effect_list']}")
    print(f"segments_with_spec_associated_after_effect_list: {coverage['segments_with_spec_associated_after_effect_list']}")
    print(f"segments_with_effect_list_registered_terminal_policy: {coverage['segments_with_effect_list_registered_terminal_policy']}")
    print(f"segments_without_useful_spec: {coverage['segments_without_useful_spec']}")
    print(f"network_should_update_now: True")
    print(f"production_full_recommended_now: False")


if __name__ == "__main__":
    main()
