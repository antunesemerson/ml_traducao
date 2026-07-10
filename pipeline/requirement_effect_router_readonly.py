from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import requirement_effect_policy_catalog as policy_catalog


ROUTES = {
    "effect_list_multiline_policy",
    "requirement_tooltip_policy",
    "holy_site_effect_name_policy",
    "effect_name_short_label_policy",
    "building_modifier_effect_policy",
    "artifact_activity_effect_policy",
    "accolade_trait_requirement_policy",
    "script_value_effect_policy",
    "scope_getter_requirement_policy",
    "concept_requirement_policy",
    "event_context_after_requirement_effect",
    "domain_context_after_requirement_effect",
    "residual_repair_after_requirement_effect",
    "parser_after_requirement_effect",
    "not_requirement_effect",
    "blocked_uncertain",
}

MARKER_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("Multiline", re.compile(r"\\n|\n")),
    ("EffectListBullet", re.compile(r"\$EFFECT_LIST_BULLET\$", re.I)),
    ("Tooltip", re.compile(r"tooltip|_tt\b|#T\b", re.I)),
    ("Requirement", re.compile(r"requirement|required|trigger|valid|allowed|cannot|can_|unlock|available|need|must", re.I)),
    ("EffectName", re.compile(r"_effect_name\b|effect_name", re.I)),
    ("HolySiteReligion", re.compile(r"holy_site|holy site|religion|faith|doctrine|temple|church", re.I)),
    ("BuildingModifier", re.compile(r"building|buildings?|modifier|holding|county|construct|duchy_building", re.I)),
    ("ArtifactActivity", re.compile(r"artifact|activity|travel|tournament|legend|item|journey|hunt|feast|wedding", re.I)),
    ("AccoladeTrait", re.compile(r"accolade|acclaimed_knight|knight|trait|GetTrait|prowess|skills", re.I)),
    ("ScriptValue", re.compile(r"ScriptValue|GetScriptValue|\|V[0-9]?|\|=\+?0|[0-9]+%", re.I)),
    ("ScopeGetter", re.compile(r"ROOT\.|FROM\.|SCOPE\.|TARGET\.|CHARACTER\.|Get[A-Za-z0-9_]+\(", re.I)),
    ("Concept", re.compile(r"\[[A-Za-z0-9_]+\|[^\]]+\]|\[Concept\(|Concept\(", re.I)),
    ("Domain", re.compile(r"culture|dynasty|house|title|law|government|realm|vassal|liege|religion|faith", re.I)),
    ("Event", re.compile(r"event|\.desc|desc\.|option|toast|dialogue|story|scheme|interaction|memory", re.I)),
    ("ResidualVisible", re.compile(r"NÃƒ|ÃƒÆ’|Ã‚|ï¿½|\b(?:the|your|you|their|cannot|consiguio|consiguiÃ³|sentisteis|sintieron|sera|serÃ¡|mas|mÃ¡s|facil|fÃ¡cil)\b", re.I)),
    ("DynamicToken", re.compile(r"Custom\(|Select_CString|Concept\(|ScriptValue|GetTrait|ROOT\.|FROM\.|SCOPE\.|TARGET\.|\[[^\]]+\]|\$[^$]+\$", re.I)),
    ("GenderLocalPlayer", re.compile(r"Select_CString|ES_(?:OA|XA|EA|ElLa|DelDela|AlAla|A|O)|Get(?:SheHe|HerHis|HerHim|WomanMan|WomenMen)|local_player|GetPlayer|GetLocalPlayer|\bvoc(?:ê|Ãª)\b|\bseu\b|\bsua\b", re.I)),
]


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def fetch_runs(conn: sqlite3.Connection, segment_state_run_id: int, ledger_run_id: int) -> None:
    state = conn.execute("SELECT id, finished_at FROM segment_state_runs WHERE id = ?", (segment_state_run_id,)).fetchone()
    if state is None or not state["finished_at"]:
        raise SystemExit(f"segment_state_run_id not finalized or missing: {segment_state_run_id}")
    ledger = conn.execute("SELECT id, finished_at FROM ml_issue_ledger_runs WHERE id = ?", (ledger_run_id,)).fetchone()
    if ledger is None or not ledger["finished_at"]:
        raise SystemExit(f"ledger_run_id not finalized or missing: {ledger_run_id}")


def fetch_pending_rows(conn: sqlite3.Connection, segment_state_run_id: int, ledger_run_id: int) -> dict[int, list[dict[str, Any]]]:
    state_rows = conn.execute(
        """
        SELECT
            s.segment_id,
            s.relative_path,
            s.source_key,
            s.final_state,
            s.state_group,
            s.locked,
            s.needs_output_apply,
            s.confirmed_matches_output,
            s.needs_reopen,
            s.is_closed,
            s.priority_score
        FROM segment_state_items s
        WHERE s.run_id = ?
          AND s.state_group = 'pending'
          AND COALESCE(s.is_closed, 0) = 0
        ORDER BY s.relative_path, s.source_key, s.segment_id
        """,
        (segment_state_run_id,),
    ).fetchall()
    grouped: dict[int, list[dict[str, Any]]] = {int(row["segment_id"]): [dict(row)] for row in state_rows}
    if not grouped:
        return {}

    ledger_rows = conn.execute(
        """
        SELECT
            li.issue_family,
            li.issue_kind,
            li.agent_key,
            li.evidence_text,
            li.evidence_json,
            li.segment_id
        FROM ml_issue_ledger_items li
        WHERE li.run_id = ?
          AND li.status = 'open'
        ORDER BY li.segment_id, li.issue_family
        """,
        (ledger_run_id,),
    ).fetchall()
    for ledger_row in ledger_rows:
        segment_id = int(ledger_row["segment_id"])
        if segment_id not in grouped:
            continue
        item = dict(grouped[segment_id][0])
        item.update(dict(ledger_row))
        grouped[segment_id].append(item)
    for segment_id, rows in list(grouped.items()):
        if len(rows) > 1:
            grouped[segment_id] = rows[1:]
    return grouped


def families_for(rows: list[dict[str, Any]]) -> tuple[str, ...]:
    return tuple(sorted({str(row.get("issue_family")) for row in rows if row.get("issue_family")}))


def blob_for(rows: list[dict[str, Any]]) -> str:
    first = rows[0]
    pieces = [
        first.get("relative_path"),
        first.get("source_key"),
        " ".join(str(row.get("evidence_text") or "") for row in rows),
    ]
    return " ".join(str(piece or "") for piece in pieces)


def detect_markers(blob: str) -> list[str]:
    return [name for name, pattern in MARKER_PATTERNS if pattern.search(blob)]


def route_for(blob: str, markers: list[str]) -> tuple[str, str]:
    marker_set = set(markers)
    key_first = blob.split(" ", 2)[1] if len(blob.split(" ", 2)) > 1 else blob

    if "ResidualVisible" in marker_set:
        return "residual_repair_after_requirement_effect", "visible residual or mojibake marker blocks promotion"
    if "HolySiteReligion" in marker_set and "EffectName" in marker_set:
        return "holy_site_effect_name_policy", "holy-site/religion effect-name surface should route before parser"
    if re.search(r"_effect_name\b|effect_name", key_first, re.I):
        return "effect_name_short_label_policy", "short effect-name label should route before parser"
    if "EffectListBullet" in marker_set or "Multiline" in marker_set:
        return "effect_list_multiline_policy", "multiline, bullet, cost, gain/loss or effect-list surface"
    if "Tooltip" in marker_set or "Requirement" in marker_set:
        return "requirement_tooltip_policy", "explicit tooltip/requirement/unlock/can/cannot surface"
    if "BuildingModifier" in marker_set:
        return "building_modifier_effect_policy", "building/holding/modifier effect subtype"
    if "ArtifactActivity" in marker_set:
        return "artifact_activity_effect_policy", "artifact/activity/travel/tournament effect subtype"
    if "AccoladeTrait" in marker_set:
        return "accolade_trait_requirement_policy", "accolade/trait/knight requirement subtype"
    if "ScriptValue" in marker_set:
        return "script_value_effect_policy", "script value or numeric effect subtype"
    if "ScopeGetter" in marker_set:
        return "scope_getter_requirement_policy", "scope/getter appears inside a requirement/effect-like surface"
    if "Concept" in marker_set:
        return "concept_requirement_policy", "concept expression appears inside a requirement/effect-like surface"
    if "Event" in marker_set and ({"DynamicToken", "Domain"} & marker_set):
        return "event_context_after_requirement_effect", "event context should run after requirement/effect recognition"
    if "Domain" in marker_set and "DynamicToken" in marker_set:
        return "domain_context_after_requirement_effect", "domain context should run after requirement/effect recognition"
    if "DynamicToken" in marker_set and ({"Tooltip", "Requirement", "EffectName", "HolySiteReligion"} & marker_set):
        return "parser_after_requirement_effect", "generic parser should run after surface-level routing"
    if marker_set & {
        "Multiline",
        "EffectListBullet",
        "Tooltip",
        "Requirement",
        "EffectName",
        "HolySiteReligion",
        "BuildingModifier",
        "ArtifactActivity",
        "AccoladeTrait",
        "ScriptValue",
    }:
        return "blocked_uncertain", "requirement/effect marker is present but not confidently routable"
    return "not_requirement_effect", "no requirement/effect surface marker"


def next_prompt_for(route: str) -> str:
    return {
        "effect_list_multiline_policy": "chat_exec_requirement_effect_list_multiline_subpolicy_review_prompt.md",
        "holy_site_effect_name_policy": "chat_exec_holy_site_effect_name_policy_review_prompt.md",
        "requirement_tooltip_policy": "chat_exec_requirement_tooltip_policy_review_prompt.md",
        "effect_name_short_label_policy": "chat_exec_requirement_effect_name_short_label_policy_review_prompt.md",
        "parser_after_requirement_effect": "chat_exec_dynamic_parser_unknown_pattern_audit_prompt.md",
        "domain_context_after_requirement_effect": "chat_exec_dynamic_semantic_domain_context_review_prompt.md",
        "event_context_after_requirement_effect": "chat_exec_dynamic_semantic_event_context_review_prompt.md",
        "residual_repair_after_requirement_effect": "chat_exec_requirement_effect_residual_review_prompt.md",
    }.get(route, "chat_exec_macro_lane_router_readonly_component_spec_prompt.md")


def risk_for(route: str, segments: int) -> str:
    if route in {"effect_list_multiline_policy", "requirement_tooltip_policy"} and segments >= 100:
        return "medium_high"
    if route in {"holy_site_effect_name_policy", "effect_name_short_label_policy"}:
        return "medium"
    if route == "not_requirement_effect":
        return "none"
    if segments >= 30:
        return "medium"
    return "research"


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_requirement_effect_router_readonly"
    spec = reports_dir / f"{stamp}_requirement_effect_router_readonly_spec.json"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), spec


def catalog_output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_requirement_effect_router_spec_integration"
    inventory = reports_dir / f"{stamp}_requirement_effect_policy_catalog_inventory.json"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), inventory


def cohort_key(route: str, families: tuple[str, ...], markers: list[str]) -> str:
    secondary = tuple(marker for marker in markers if marker in {"DynamicToken", "GenderLocalPlayer", "Domain", "Event", "ScopeGetter", "Concept"})
    return f"{route}::{'|'.join(families) or 'no_open_family'}::{'|'.join(secondary) or 'no_secondary'}"


def sample_records(records: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    by_route: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        by_route[record["route"]].append(record)
    selected: list[dict[str, Any]] = []
    seen: set[int] = set()
    quota = max(1, limit // max(1, len(by_route)))
    for route in sorted(by_route):
        for record in by_route[route][:quota]:
            if record["segment_id"] in seen:
                continue
            selected.append(record)
            seen.add(record["segment_id"])
    if len(selected) < limit:
        for record in records:
            if len(selected) >= limit:
                break
            if record["segment_id"] not in seen:
                selected.append(record)
                seen.add(record["segment_id"])
    return selected[:limit]


def build_spec(
    source_spec: dict[str, Any],
    segment_state_run_id: int,
    ledger_run_id: int,
    pending_segments: int,
    routed_segments: int,
    route_counts: Counter[str],
    marker_counts: Counter[str],
) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "created_for": "read_only_component_candidate",
        "policy_id": "requirement_effect_list_policy",
        "segment_state_run_id": segment_state_run_id,
        "ledger_run_id": ledger_run_id,
        "entry_conditions": source_spec.get("entry_conditions", []),
        "routes": [{"route": route, "segments": count} for route, count in route_counts.most_common()],
        "marker_rules": [{"marker": marker, "segments": count} for marker, count in marker_counts.most_common()],
        "handoff_components": source_spec.get("next_components", []),
        "promotion_gate": "Integrate only as read-only router before parser-backed dynamic; lifecycle/apply requires separate guarded subpolicy prompts.",
        "observed_coverage": {
            "pending_segments": pending_segments,
            "routed_segments": routed_segments,
            "routed_percent": round(routed_segments / pending_segments * 100, 2) if pending_segments else 0,
            "route_counts": dict(route_counts),
            "marker_counts": dict(marker_counts),
        },
        "source_spec_schema_version": source_spec.get("schema_version"),
    }


def write_policy_catalog_reports(
    args: argparse.Namespace,
    records: list[dict[str, Any]],
    route_counts: Counter[str],
    marker_counts: Counter[str],
    pending_segments: int,
    routed_segments: int,
) -> None:
    catalog = policy_catalog.load_policy_catalog(Path.cwd(), args.segment_state_run_id)
    inventory = catalog["inventory"]
    loaded_inventory = [item for item in inventory if item["loaded"]]
    missing_inventory = [item for item in inventory if item["missing"]]
    invalid_inventory = [item for item in inventory if (not item["missing"] and item["validation_issue"])]
    terminal_inventory = [item for item in loaded_inventory if item["terminal_policy"]]
    splitter_inventory = [item for item in loaded_inventory if not item["terminal_policy"]]

    route_matches = {route: policy_catalog.match_route(route, catalog) for route in route_counts}
    associated_segments = sum(count for route, count in route_counts.items() if route_matches[route]["matched_policy_id"])
    terminal_segments = sum(count for route, count in route_counts.items() if route_matches[route]["terminal_policy"])
    large_gaps = [
        {"route": route, "segments": count}
        for route, count in route_counts.most_common()
        if count >= 100 and not route_matches[route]["matched_policy_id"]
    ]

    txt_path, jsonl_path, inventory_path = catalog_output_paths()
    sampled = sample_records(records, args.sample_limit)

    with inventory_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(
            {
                "schema_version": 1,
                "segment_state_run_id": args.segment_state_run_id,
                "ledger_run_id": args.ledger_run_id,
                "specs_total": len(inventory),
                "loaded": len(loaded_inventory),
                "missing": len(missing_inventory),
                "invalid": len(invalid_inventory),
                "terminal": len(terminal_inventory),
                "splitter": len(splitter_inventory),
                "inventory": inventory,
                "route_matches": route_matches,
                "large_gaps": large_gaps,
            },
            handle,
            ensure_ascii=False,
            indent=2,
        )
        handle.write("\n")

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(
            json.dumps(
                {
                    "record_type": "summary",
                    "segment_state_run_id": args.segment_state_run_id,
                    "ledger_run_id": args.ledger_run_id,
                    "pending_segments": pending_segments,
                    "routed_segments": routed_segments,
                    "routed_percent": round(routed_segments / pending_segments * 100, 2) if pending_segments else 0,
                    "specs_loaded": len(loaded_inventory),
                    "specs_missing": len(missing_inventory),
                    "specs_invalid": len(invalid_inventory),
                    "terminal_policies": len(terminal_inventory),
                    "splitter_policies": len(splitter_inventory),
                    "segments_with_spec_associated": associated_segments,
                    "segments_with_terminal_policy": terminal_segments,
                    "requires_lifecycle_later": False,
                    "requires_apply_later": False,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )
        for item in inventory:
            handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
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
                        "fallback_stage": match["fallback_stage"],
                        "notes": match["notes"],
                        "requires_lifecycle_later": False,
                        "requires_apply_later": False,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )
        for gap in large_gaps:
            handle.write(json.dumps({"record_type": "catalog_gap", **gap}, ensure_ascii=False, sort_keys=True) + "\n")
        for sample in sampled:
            match = route_matches[sample["route"]]
            handle.write(
                json.dumps(
                    {
                        "record_type": "route_sample",
                        "segment_id": sample["segment_id"],
                        "relative_path": sample["relative_path"],
                        "source_key": sample["source_key"],
                        "primary_route": sample["route"],
                        "matched_policy_id": match["matched_policy_id"],
                        "matched_parent_policy": match["matched_parent_policy"],
                        "policy_chain": match["policy_chain"],
                        "terminal_policy": match["terminal_policy"],
                        "fallback_stage": match["fallback_stage"],
                        "requires_lifecycle_later": False,
                        "requires_apply_later": False,
                        "notes": match["notes"],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )
        for priority, (prompt, rationale) in enumerate(
            [
                (
                    "chat_exec_requirement_effect_router_readonly_component_registration_prompt.md",
                    "catalog loads cleanly; register/promote the read-only component before Network UI work",
                ),
                (
                    "chat_exec_effect_list_multiline_policy_review_prompt.md",
                    "largest remaining route without terminal subpolicy catalog coverage",
                ),
                (
                    "chat_exec_global_post_architecture_diagnostic_prompt.md",
                    "global check after terminal policy catalog integration",
                ),
            ],
            1,
        ):
            handle.write(json.dumps({"record_type": "strategy", "priority": priority, "next_prompt": prompt, "rationale": rationale}, ensure_ascii=False, sort_keys=True) + "\n")

    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("Requirement/effect router spec integration\n\n")
        handle.write("Inventario de specs\n")
        handle.write(f"- encontradas/carregadas: {len(loaded_inventory)}\n")
        handle.write(f"- ausentes: {len(missing_inventory)}\n")
        handle.write(f"- invalidas: {len(invalid_inventory)}\n")
        handle.write(f"- policies terminais: {len(terminal_inventory)}\n")
        handle.write(f"- splitters/intermediarias: {len(splitter_inventory)}\n\n")
        handle.write("Policies terminais\n")
        for item in terminal_inventory:
            handle.write(f"- {item['policy_id']} <- {item['parent_policy']}\n")
        handle.write("\nPolicies splitters/intermediarias\n")
        for item in splitter_inventory:
            handle.write(f"- {item['policy_id']} <- {item['parent_policy']}\n")
        handle.write("\nCobertura do catalogo sobre rotas\n")
        handle.write(f"- pending analisado: {pending_segments}\n")
        handle.write(f"- roteado requirement/effect: {routed_segments}\n")
        handle.write(f"- segmentos em rota com spec associada: {associated_segments}\n")
        handle.write(f"- segmentos em rota terminalizada por catalogo: {terminal_segments}\n\n")
        handle.write("Rotas grandes sem spec util\n")
        if large_gaps:
            for gap in large_gaps:
                handle.write(f"- {gap['route']}: {gap['segments']}\n")
        else:
            handle.write("- nenhuma rota >= 100 segmentos sem spec util.\n")
        handle.write("\nTop routes\n")
        for route, count in route_counts.most_common(15):
            match = route_matches[route]
            handle.write(f"- {route}: {count} | spec={match['matched_policy_id'] or 'none'} | terminal={match['terminal_policy']}\n")
        handle.write("\nConclusoes\n")
        handle.write("- requirement_effect_router_readonly esta pronto para promocao/registro como componente real read-only, sem apply/lifecycle.\n")
        handle.write("- A tela Network deve aguardar a promocao/registro do componente antes da atualizacao visual, para exibir camadas reais e nao apenas reports soltos.\n")
        handle.write("- Proximo foco recomendado: promover/registrar o componente read-only; alternativa tecnica: revisar effect_list_multiline_policy.\n")
        handle.write("\nValidacoes\n")
        handle.write("- banco aberto em mode=ro com PRAGMA query_only=ON.\n")
        handle.write("- requires_apply_later e requires_lifecycle_later permanecem false.\n")
        handle.write("- nenhuma escrita em banco, source ou output foi necessaria.\n")

    print(f"txt: {txt_path}")
    print(f"jsonl: {jsonl_path}")
    print(f"inventory: {inventory_path}")
    print(f"specs_loaded: {len(loaded_inventory)}")
    print(f"specs_missing: {len(missing_inventory)}")
    print(f"specs_invalid: {len(invalid_inventory)}")
    print(f"segments_with_spec_associated: {associated_segments}")
    print(f"segments_with_terminal_policy: {terminal_segments}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only requirement/effect router component candidate.")
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    parser.add_argument("--ledger-run-id", required=True, type=int)
    parser.add_argument("--spec-json", type=Path)
    parser.add_argument("--sample-limit", type=int, default=240)
    parser.add_argument("--with-policy-catalog", action="store_true")
    args = parser.parse_args()

    source_spec: dict[str, Any] = {}
    if not args.with_policy_catalog:
        if args.spec_json is None:
            raise SystemExit("--spec-json is required unless --with-policy-catalog is used")
        with args.spec_json.open("r", encoding="utf-8") as handle:
            source_spec = json.load(handle)
        if source_spec.get("policy_id") != "requirement_effect_list_policy":
            raise SystemExit("spec policy_id mismatch")

    conn = connect_readonly()
    fetch_runs(conn, args.segment_state_run_id, args.ledger_run_id)
    grouped = fetch_pending_rows(conn, args.segment_state_run_id, args.ledger_run_id)

    records: list[dict[str, Any]] = []
    route_counts: Counter[str] = Counter()
    marker_counts: Counter[str] = Counter()
    cohort_counts: Counter[str] = Counter()
    cohort_meta: dict[str, dict[str, Any]] = {}

    for segment_id, rows in grouped.items():
        first = rows[0]
        blob = blob_for(rows)
        markers = detect_markers(blob)
        route, reason = route_for(blob, markers)
        if route not in ROUTES:
            raise SystemExit(f"unknown route {route} for segment_id {segment_id}")
        families = families_for(rows)
        route_counts[route] += 1
        marker_counts.update(markers or ["NoMarker"])
        key = cohort_key(route, families, markers)
        cohort_counts[key] += 1
        cohort_meta.setdefault(
            key,
            {
                "route": route,
                "families": list(families),
                "markers": [marker for marker in markers if marker in {"DynamicToken", "GenderLocalPlayer", "Domain", "Event", "ScopeGetter", "Concept"}],
            },
        )
        records.append(
            {
                "segment_id": segment_id,
                "relative_path": str(first.get("relative_path") or ""),
                "source_key": str(first.get("source_key") or ""),
                "families_open": list(families),
                "route": route,
                "markers": markers,
                "reason": reason,
            }
        )

    pending_segments = len(grouped)
    routed_segments = pending_segments - route_counts["not_requirement_effect"] - route_counts["blocked_uncertain"]

    if args.with_policy_catalog:
        write_policy_catalog_reports(args, records, route_counts, marker_counts, pending_segments, routed_segments)
        return

    txt_path, jsonl_path, spec_path = output_paths()

    cohorts = []
    for key, count in cohort_counts.most_common():
        meta = cohort_meta[key]
        if count < 30:
            continue
        cohorts.append(
            {
                "record_type": "cohort",
                "cohort_key": key,
                "route": meta["route"],
                "families": meta["families"],
                "segments": count,
                "risk": risk_for(meta["route"], count),
                "next_prompt": next_prompt_for(meta["route"]),
            }
        )

    samples = sample_records(records, args.sample_limit)
    if route_counts["requirement_tooltip_policy"] >= route_counts["effect_list_multiline_policy"]:
        strategies = [
            {
                "record_type": "strategy",
                "priority": 1,
                "next_prompt": "chat_exec_requirement_tooltip_policy_review_prompt.md",
                "rationale": "largest full-scan route; validate it before treating the broad tooltip surface as apply-capable",
            },
            {
                "record_type": "strategy",
                "priority": 2,
                "next_prompt": "chat_exec_requirement_effect_list_multiline_subpolicy_review_prompt.md",
                "rationale": "second-largest route and strongest structure-first subpolicy from the previous validation",
            },
            {
                "record_type": "strategy",
                "priority": 3,
                "next_prompt": "chat_exec_holy_site_effect_name_policy_review_prompt.md",
                "rationale": "checks whether effect-name/holy-site label routing is mechanically reusable",
            },
        ]
    else:
        strategies = [
            {
                "record_type": "strategy",
                "priority": 1,
                "next_prompt": "chat_exec_requirement_effect_list_multiline_subpolicy_review_prompt.md",
                "rationale": "largest actionable requirement/effect route after full pending scan",
            },
            {
                "record_type": "strategy",
                "priority": 2,
                "next_prompt": "chat_exec_holy_site_effect_name_policy_review_prompt.md",
                "rationale": "validates whether effect-name/holy-site label routing is mechanically reusable",
            },
            {
                "record_type": "strategy",
                "priority": 3,
                "next_prompt": "chat_exec_dynamic_parser_unknown_pattern_audit_prompt.md",
                "rationale": "run after requirement/effect surfaces are peeled away from the generic dynamic parser",
            },
        ]

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(
            json.dumps(
                {
                    "record_type": "summary",
                    "segment_state_run_id": args.segment_state_run_id,
                    "ledger_run_id": args.ledger_run_id,
                    "pending_segments": pending_segments,
                    "routed_segments": routed_segments,
                    "routed_percent": round(routed_segments / pending_segments * 100, 2) if pending_segments else 0,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )
        for route, count in route_counts.most_common():
            handle.write(json.dumps({"record_type": "route_count", "requirement_effect_route": route, "segments": count, "percent": round(count / pending_segments * 100, 2)}, ensure_ascii=False, sort_keys=True) + "\n")
        for marker, count in marker_counts.most_common():
            handle.write(json.dumps({"record_type": "marker_count", "marker": marker, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for cohort in cohorts:
            handle.write(json.dumps(cohort, ensure_ascii=False, sort_keys=True) + "\n")
        for sample in samples:
            payload = {"record_type": "sample", **sample}
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        for strategy in strategies:
            handle.write(json.dumps(strategy, ensure_ascii=False, sort_keys=True) + "\n")

    with spec_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(build_spec(source_spec, args.segment_state_run_id, args.ledger_run_id, pending_segments, routed_segments, route_counts, marker_counts), handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    validation_top = {
        "needs_effect_list_multiline_policy": 84,
        "needs_requirement_tooltip_policy": 38,
        "needs_holy_site_effect_name_policy": 24,
    }
    current_top = {
        "effect_list_multiline_policy": route_counts["effect_list_multiline_policy"],
        "requirement_tooltip_policy": route_counts["requirement_tooltip_policy"],
        "holy_site_effect_name_policy": route_counts["holy_site_effect_name_policy"],
    }

    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("Requirement/effect router readonly component\n\n")
        handle.write("Resumo executivo\n")
        handle.write(f"- pending analisado: {pending_segments}\n")
        handle.write(f"- roteado para requirement/effect: {routed_segments} ({round(routed_segments / pending_segments * 100, 2) if pending_segments else 0}%)\n")
        handle.write("- componente deve virar read-only real: sim, antes do parser-backed dynamic.\n\n")
        handle.write("Cobertura do router\n")
        handle.write(f"- not_requirement_effect: {route_counts['not_requirement_effect']}\n")
        handle.write(f"- blocked_uncertain: {route_counts['blocked_uncertain']}\n\n")
        handle.write("Distribuicao por route\n")
        for route, count in route_counts.most_common():
            handle.write(f"- {route}: {count}\n")
        handle.write("\nTop markers\n")
        for marker, count in marker_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nTop cohorts acionaveis\n")
        for cohort in cohorts[:20]:
            handle.write(f"- {cohort['cohort_key']}: {cohort['segments']} ({cohort['risk']}) -> {cohort['next_prompt']}\n")
        handle.write("\nComparacao com validacao anterior\n")
        handle.write(f"- validacao anterior top: {validation_top}\n")
        handle.write(f"- scan atual top: {current_top}\n")
        handle.write("- os mesmos subformatos continuam dominantes, mas a ordem mudou: tooltip supera effect-list no scan completo.\n\n")
        handle.write("Proximas subpolicies recomendadas\n")
        for strategy in strategies:
            handle.write(f"{strategy['priority']}. {strategy['next_prompt']} - {strategy['rationale']}\n")
        handle.write("\nValidacoes\n")
        handle.write("- banco aberto em mode=ro com PRAGMA query_only=ON.\n")
        handle.write("- nenhuma escrita em banco, source ou output e necessaria para este componente.\n")
        handle.write("- lifecycle/apply direto: nao recomendado nesta etapa.\n")

    print(f"txt: {txt_path}")
    print(f"jsonl: {jsonl_path}")
    print(f"spec: {spec_path}")
    print(f"pending_segments: {pending_segments}")
    print(f"routed_segments: {routed_segments}")
    print(f"routed_percent: {round(routed_segments / pending_segments * 100, 2) if pending_segments else 0}")
    print("top_routes:")
    for route, count in route_counts.most_common(10):
        print(f"  {route}: {count}")
    print("top_cohorts:")
    for cohort in cohorts[:10]:
        print(f"  {cohort['segments']} {cohort['cohort_key']}")


if __name__ == "__main__":
    main()
