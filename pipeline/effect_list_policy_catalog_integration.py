from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import requirement_effect_policy_catalog as policy_catalog
import requirement_effect_router_readonly as router


EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76
SOURCE = "effect_list_policy_catalog_integration_v1"
PREVIOUS_INVENTORY = "reports/20260621_235338_197498_requirement_effect_policy_catalog_inventory.json"

EFFECT_LIST_SPEC_PATHS = {
    "reports/20260622_004245_181542_effect_list_multiline_policy_spec.json",
    "reports/20260622_014609_044703_effect_list_artifact_activity_policy_spec.json",
    "reports/20260622_130721_601727_effect_list_gender_local_player_policy_spec.json",
    "reports/20260622_133901_719476_effect_list_trait_accolade_policy_spec.json",
    "reports/20260622_141149_802441_effect_list_script_value_policy_spec.json",
    "reports/20260622_144059_266106_effect_list_concept_policy_spec.json",
    "reports/20260622_020638_524121_artifact_item_effect_policy_spec.json",
    "reports/20260622_023205_601243_artifact_item_scope_getter_policy_spec.json",
    "reports/20260622_113242_032221_artifact_activity_gender_local_player_policy_spec.json",
    "reports/20260622_123357_154242_artifact_activity_script_value_policy_spec.json",
}

TERMINAL_EFFECT_LIST_POLICIES = {
    "effect_list_gender_local_player_policy",
    "effect_list_trait_accolade_policy",
    "effect_list_script_value_policy",
    "effect_list_concept_policy",
    "artifact_activity_gender_local_player_policy",
    "artifact_activity_script_value_policy",
}

SPLITTER_EFFECT_LIST_POLICIES = {
    "effect_list_multiline_policy",
    "effect_list_artifact_activity_policy",
    "artifact_item_effect_policy",
    "artifact_item_scope_getter_policy",
}

REGISTERED_TERMINAL_GUARDS = {
    "artifact_activity_gender_local_player_policy",
    "effect_list_gender_local_player_policy",
    "effect_list_trait_accolade_policy",
    "effect_list_script_value_policy",
    "effect_list_concept_policy",
}


def output_paths() -> tuple[Path, Path, Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    integration = reports_dir / f"{stamp}_effect_list_policy_catalog_integration"
    inventory = reports_dir / f"{stamp}_effect_list_policy_catalog_inventory.json"
    router_after = reports_dir / f"{stamp}_requirement_effect_router_readonly_after_effect_list"
    return (
        integration.with_suffix(".txt"),
        integration.with_suffix(".jsonl"),
        inventory,
        router_after.with_suffix(".txt"),
        router_after.with_suffix(".jsonl"),
    )


def connect_readonly() -> sqlite3.Connection:
    conn = router.connect_readonly()
    probe = conn.execute("PRAGMA query_only").fetchone()
    if int(probe[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def registered_agents(conn: sqlite3.Connection, keys: set[str]) -> set[str]:
    if not keys:
        return set()
    placeholders = ",".join("?" for _ in keys)
    rows = conn.execute(
        f"""
        SELECT agent_key
        FROM ml_agent_registry
        WHERE agent_key IN ({placeholders})
          AND status = 'active'
        """,
        tuple(sorted(keys)),
    ).fetchall()
    return {str(row["agent_key"]) for row in rows}


def load_previous_inventory(project_root: Path) -> dict[str, Any]:
    path = project_root / PREVIOUS_INVENTORY
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def route_records(segment_state_run_id: int, ledger_run_id: int) -> tuple[list[dict[str, Any]], Counter[str], int, int]:
    with connect_readonly() as conn:
        router.fetch_runs(conn, segment_state_run_id, ledger_run_id)
        grouped = router.fetch_pending_rows(conn, segment_state_run_id, ledger_run_id)
    records: list[dict[str, Any]] = []
    route_counts: Counter[str] = Counter()
    for segment_id, rows in grouped.items():
        first = rows[0]
        blob = router.blob_for(rows)
        markers = router.detect_markers(blob)
        route, reason = router.route_for(blob, markers)
        route_counts[route] += 1
        records.append(
            {
                "segment_id": segment_id,
                "relative_path": str(first.get("relative_path") or ""),
                "source_key": str(first.get("source_key") or ""),
                "families_open": list(router.families_for(rows)),
                "route": route,
                "markers": markers,
                "reason": reason,
            }
        )
    pending_segments = len(grouped)
    routed_segments = pending_segments - route_counts["not_requirement_effect"] - route_counts["blocked_uncertain"]
    return records, route_counts, pending_segments, routed_segments


def effect_list_subroute(markers: list[str]) -> tuple[str, str]:
    marker_set = set(markers)
    if {"ArtifactActivity", "GenderLocalPlayer"} <= marker_set:
        return "artifact_activity_gender_local_player_policy", "artifact/activity plus gender/local-player guard"
    if {"ArtifactActivity", "ScriptValue"} <= marker_set:
        return "artifact_activity_script_value_policy", "artifact/activity plus ScriptValue guard"
    if "ArtifactActivity" in marker_set:
        return "effect_list_artifact_activity_policy", "artifact/activity splitter still required"
    if "GenderLocalPlayer" in marker_set:
        return "effect_list_gender_local_player_policy", "registered gender/local-player terminal guard"
    if "AccoladeTrait" in marker_set:
        return "effect_list_trait_accolade_policy", "registered trait/accolade terminal guard"
    if "ScriptValue" in marker_set:
        return "effect_list_script_value_policy", "registered ScriptValue terminal guard"
    if "Concept" in marker_set:
        return "effect_list_concept_policy", "registered concept terminal guard"
    if "Event" in marker_set:
        return "effect_list_multiline_terminal_policy_with_event_guard", "event guard remains catalog fallback"
    if "Domain" in marker_set:
        return "effect_list_multiline_terminal_policy_with_domain_guard", "domain guard remains catalog fallback"
    return "effect_list_unclassified_or_block_policy", "fallback effect-list route without useful subpolicy spec"


def summarize_previous_coverage(previous: dict[str, Any], route_counts: Counter[str]) -> dict[str, int]:
    matches = previous.get("route_matches") or {}
    associated = 0
    terminal = 0
    for route, count in route_counts.items():
        match = matches.get(route) or {}
        if match.get("matched_policy_id"):
            associated += count
        if match.get("terminal_policy"):
            terminal += count
    return {"segments_with_spec_associated": associated, "segments_with_terminal_policy": terminal}


def write_reports(
    *,
    args: argparse.Namespace,
    catalog: dict[str, Any],
    previous_inventory: dict[str, Any],
    registered: set[str],
    records: list[dict[str, Any]],
    route_counts: Counter[str],
    pending_segments: int,
    routed_segments: int,
) -> tuple[Path, Path, Path, Path, Path, dict[str, Any]]:
    txt_path, jsonl_path, inventory_path, router_txt_path, router_jsonl_path = output_paths()

    inventory = catalog["inventory"]
    effect_items = [item for item in inventory if item["path"] in EFFECT_LIST_SPEC_PATHS]
    missing_effect = [item for item in effect_items if item["missing"]]
    invalid_effect = [item for item in effect_items if (not item["missing"] and item["validation_issue"])]
    loaded_effect = [item for item in effect_items if item["loaded"]]
    terminal_effect = [item for item in loaded_effect if item.get("catalog_role") in {"terminal_guard", "terminal_reuse"}]
    splitter_effect = [item for item in loaded_effect if item.get("catalog_role") == "splitter"]
    registered_effect = sorted({item["policy_id"] for item in loaded_effect if item["policy_id"] in registered})
    spec_only_effect = sorted({item["policy_id"] for item in loaded_effect if item["policy_id"] not in registered})

    if missing_effect:
        raise SystemExit(f"missing_effect_list_specs > 0: {[item['path'] for item in missing_effect]}")
    if invalid_effect:
        raise SystemExit(f"invalid_effect_list_specs > 0: {invalid_effect}")

    route_matches = {route: policy_catalog.match_route(route, catalog) for route in route_counts}
    previous_coverage = summarize_previous_coverage(previous_inventory, route_counts)
    associated_segments = sum(count for route, count in route_counts.items() if route_matches[route]["matched_policy_id"])
    terminal_segments = sum(count for route, count in route_counts.items() if route_matches[route]["terminal_policy"])

    effect_records = [record for record in records if record["route"] == "effect_list_multiline_policy"]
    subroute_counts: Counter[str] = Counter()
    subroute_reasons: dict[str, str] = {}
    for record in effect_records:
        subroute, reason = effect_list_subroute(record["markers"])
        subroute_counts[subroute] += 1
        subroute_reasons.setdefault(subroute, reason)
    effect_with_spec = sum(count for subroute, count in subroute_counts.items() if subroute in catalog["by_id"])
    effect_terminal = sum(count for subroute, count in subroute_counts.items() if subroute in TERMINAL_EFFECT_LIST_POLICIES)
    effect_registered_terminal = sum(count for subroute, count in subroute_counts.items() if subroute in registered)
    large_without_spec = [
        {"route": route, "segments": count}
        for route, count in route_counts.most_common()
        if count >= 100 and not route_matches[route]["matched_policy_id"]
    ]
    effect_large_without_spec = [
        {
            "subroute": subroute,
            "segments": count,
            "reason": subroute_reasons[subroute],
        }
        for subroute, count in subroute_counts.most_common()
        if count >= 30 and subroute not in catalog["by_id"]
    ]

    summary = {
        "record_type": "summary",
        "source": SOURCE,
        "segment_state_run_id": args.segment_state_run_id,
        "ledger_run_id": args.ledger_run_id,
        "specs_effect_list_loaded": len(loaded_effect),
        "missing_effect_list_specs": len(missing_effect),
        "invalid_effect_list_specs": len(invalid_effect),
        "terminal_effect_list_policies": len(terminal_effect),
        "splitter_effect_list_policies": len(splitter_effect),
        "registered_effect_list_specs": len(registered_effect),
        "spec_only_effect_list_specs": len(spec_only_effect),
        "pending_segments_analyzed": pending_segments,
        "segments_with_requirement_effect_route": routed_segments,
        "segments_with_effect_list_route": route_counts["effect_list_multiline_policy"],
        "segments_with_spec_effect_list_associated": effect_with_spec,
        "segments_with_terminal_effect_list_policy": effect_terminal,
        "segments_with_registered_terminal_effect_list_policy": effect_registered_terminal,
        "segments_with_spec_associated_before": previous_coverage["segments_with_spec_associated"],
        "segments_with_spec_associated_after": associated_segments,
        "segments_with_terminal_policy_before": previous_coverage["segments_with_terminal_policy"],
        "segments_with_terminal_policy_after": terminal_segments,
        "requires_apply_later": 0,
        "requires_lifecycle_later": 0,
        "auto_apply_allowed": 0,
        "production_release_allowed": 0,
        "lifecycle_allowed": 0,
        "database_query_only": True,
    }

    with inventory_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(
            {
                "schema_version": 1,
                "source": SOURCE,
                "segment_state_run_id": args.segment_state_run_id,
                "ledger_run_id": args.ledger_run_id,
                "effect_list_inventory": effect_items,
                "registered_effect_list_specs": registered_effect,
                "spec_only_effect_list_specs": spec_only_effect,
                "route_matches": route_matches,
                "effect_list_subroute_counts": [
                    {
                        "subroute": subroute,
                        "segments": count,
                        "cataloged": subroute in catalog["by_id"],
                        "terminal_policy": subroute in TERMINAL_EFFECT_LIST_POLICIES,
                        "registered": subroute in registered,
                        "reason": subroute_reasons[subroute],
                    }
                    for subroute, count in subroute_counts.most_common()
                ],
                "large_routes_without_spec": large_without_spec,
                "effect_list_large_subroutes_without_spec": effect_large_without_spec,
                "summary": summary,
            },
            handle,
            ensure_ascii=False,
            indent=2,
        )
        handle.write("\n")

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(summary, ensure_ascii=False, sort_keys=True) + "\n")
        for item in effect_items:
            handle.write(json.dumps({"record_type": "effect_list_spec", **item}, ensure_ascii=False, sort_keys=True) + "\n")
        for subroute, count in subroute_counts.most_common():
            handle.write(
                json.dumps(
                    {
                        "record_type": "effect_list_subroute_count",
                        "subroute": subroute,
                        "segments": count,
                        "cataloged": subroute in catalog["by_id"],
                        "terminal_policy": subroute in TERMINAL_EFFECT_LIST_POLICIES,
                        "registered": subroute in registered,
                        "reason": subroute_reasons[subroute],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )
        for gap in large_without_spec:
            handle.write(json.dumps({"record_type": "large_route_without_spec", **gap}, ensure_ascii=False, sort_keys=True) + "\n")
        for gap in effect_large_without_spec:
            handle.write(json.dumps({"record_type": "effect_list_large_subroute_without_spec", **gap}, ensure_ascii=False, sort_keys=True) + "\n")

    with router_jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps({"record_type": "summary", **summary}, ensure_ascii=False, sort_keys=True) + "\n")
        for route, count in route_counts.most_common():
            match = route_matches[route]
            handle.write(
                json.dumps(
                    {
                        "record_type": "route_catalog_count",
                        "primary_route": route,
                        "segments": count,
                        "matched_policy_id": match["matched_policy_id"],
                        "policy_chain": match["policy_chain"],
                        "terminal_policy": match["terminal_policy"],
                        "requires_apply_later": False,
                        "requires_lifecycle_later": False,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )

    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("Effect-list policy catalog integration\n\n")
        handle.write("Specs effect-list\n")
        handle.write(f"- carregadas: {summary['specs_effect_list_loaded']}\n")
        handle.write(f"- ausentes: {summary['missing_effect_list_specs']}\n")
        handle.write(f"- invalidas: {summary['invalid_effect_list_specs']}\n")
        handle.write(f"- terminais/terminal-reuse: {summary['terminal_effect_list_policies']}\n")
        handle.write(f"- splitters/intermediarias: {summary['splitter_effect_list_policies']}\n")
        handle.write(f"- registradas no ml_agent_registry: {summary['registered_effect_list_specs']} ({', '.join(registered_effect)})\n")
        handle.write(f"- somente catalogadas: {summary['spec_only_effect_list_specs']} ({', '.join(spec_only_effect)})\n\n")
        handle.write("Cobertura\n")
        handle.write(f"- pendentes analisados: {pending_segments}\n")
        handle.write(f"- rotas requirement/effect: {routed_segments}\n")
        handle.write(f"- rota effect-list: {route_counts['effect_list_multiline_policy']}\n")
        handle.write(f"- effect-list com spec associada: {effect_with_spec}\n")
        handle.write(f"- effect-list com policy terminal: {effect_terminal}\n")
        handle.write(f"- effect-list com terminal registrada: {effect_registered_terminal}\n")
        handle.write(f"- spec associada antes/depois: {previous_coverage['segments_with_spec_associated']} -> {associated_segments}\n")
        handle.write(f"- terminal por catalogo antes/depois: {previous_coverage['segments_with_terminal_policy']} -> {terminal_segments}\n\n")
        handle.write("Subrotas effect-list\n")
        for subroute, count in subroute_counts.most_common():
            handle.write(f"- {subroute}: {count} | spec={subroute in catalog['by_id']} | terminal={subroute in TERMINAL_EFFECT_LIST_POLICIES} | registered={subroute in registered}\n")
        handle.write("\nRotas grandes ainda sem spec util\n")
        for gap in large_without_spec:
            handle.write(f"- {gap['route']}: {gap['segments']}\n")
        handle.write("\nSubrotas effect-list grandes ainda sem spec util\n")
        if effect_large_without_spec:
            for gap in effect_large_without_spec:
                handle.write(f"- {gap['subroute']}: {gap['segments']} ({gap['reason']})\n")
        else:
            handle.write("- nenhuma subrota effect-list >= 30 sem spec util.\n")
        handle.write("\nValidacoes\n")
        handle.write("- banco aberto em mode=ro com PRAGMA query_only=ON.\n")
        handle.write("- requires_apply_later=0 e requires_lifecycle_later=0.\n")
        handle.write("- auto_apply_allowed=0, production_release_allowed=0, lifecycle_allowed=0.\n")

    with router_txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("Requirement/effect router readonly after effect-list integration\n\n")
        handle.write(f"- pending analisado: {pending_segments}\n")
        handle.write(f"- requirement/effect roteado: {routed_segments}\n")
        handle.write(f"- segmentos em rota com spec associada: {associated_segments}\n")
        handle.write(f"- segmentos terminalizados por catalogo: {terminal_segments}\n")
        handle.write("\nTop routes\n")
        for route, count in route_counts.most_common(15):
            match = route_matches[route]
            handle.write(f"- {route}: {count} | spec={match['matched_policy_id'] or 'none'} | terminal={match['terminal_policy']}\n")

    return txt_path, jsonl_path, inventory_path, router_txt_path, router_jsonl_path, summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Integrate effect-list specs into the read-only requirement/effect catalog.")
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    parser.add_argument("--ledger-run-id", required=True, type=int)
    args = parser.parse_args()

    if args.segment_state_run_id != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit(f"segment_state_run_id guard failed: {args.segment_state_run_id}")
    if args.ledger_run_id != EXPECTED_LEDGER_RUN_ID:
        raise SystemExit(f"ledger_run_id guard failed: {args.ledger_run_id}")

    project_root = Path.cwd()
    catalog = policy_catalog.load_policy_catalog(project_root, args.segment_state_run_id)
    previous = load_previous_inventory(project_root)
    records, route_counts, pending_segments, routed_segments = route_records(args.segment_state_run_id, args.ledger_run_id)
    with connect_readonly() as conn:
        registered = registered_agents(conn, TERMINAL_EFFECT_LIST_POLICIES | SPLITTER_EFFECT_LIST_POLICIES)

    txt_path, jsonl_path, inventory_path, router_txt_path, router_jsonl_path, summary = write_reports(
        args=args,
        catalog=catalog,
        previous_inventory=previous,
        registered=registered,
        records=records,
        route_counts=route_counts,
        pending_segments=pending_segments,
        routed_segments=routed_segments,
    )
    print(f"txt: {txt_path}")
    print(f"jsonl: {jsonl_path}")
    print(f"inventory: {inventory_path}")
    print(f"router_txt: {router_txt_path}")
    print(f"router_jsonl: {router_jsonl_path}")
    for key in [
        "specs_effect_list_loaded",
        "missing_effect_list_specs",
        "invalid_effect_list_specs",
        "terminal_effect_list_policies",
        "splitter_effect_list_policies",
        "registered_effect_list_specs",
        "spec_only_effect_list_specs",
        "pending_segments_analyzed",
        "segments_with_requirement_effect_route",
        "segments_with_effect_list_route",
        "segments_with_spec_effect_list_associated",
        "segments_with_terminal_effect_list_policy",
        "segments_with_registered_terminal_effect_list_policy",
        "segments_with_spec_associated_before",
        "segments_with_spec_associated_after",
        "segments_with_terminal_policy_before",
        "segments_with_terminal_policy_after",
        "requires_apply_later",
        "requires_lifecycle_later",
    ]:
        print(f"{key}: {summary[key]}")


if __name__ == "__main__":
    main()
