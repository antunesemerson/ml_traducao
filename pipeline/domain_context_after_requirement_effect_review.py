from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import agent_inventory_diagnostic as agent_inventory
import db
import global_post_architecture_diagnostic as architecture_diag
import requirement_effect_router_readonly as router


SOURCE = "domain_context_after_requirement_effect_review_v1"
EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76
EXPECTED_CLOSED_COUNT = 276375
EXPECTED_PENDING_COUNT = 11725
EXPECTED_OUTPUT_APPLY_PENDING_COUNT = 0
EXPECTED_REGISTERED_AGENTS = 229
EXPECTED_OBSERVED_AGENT_KEYS = 286
EXPECTED_OPERATIONAL_AGENTS = 33
EXPECTED_DRY_RUN_AGENTS = 23
EXPECTED_SHADOW_AGENTS = 87
EXPECTED_ROUTE = "domain_context_after_requirement_effect"
EXPECTED_UNIVERSE = 174


DOMAIN_RE = re.compile(r"culture|religion|faith|doctrine|dynasty|house|title|law|government|realm|vassal|liege|artifact|activity|building|modifier|holy_site|holy site|court|war|scheme|travel|tournament|legend|memory", re.I)
REQUIREMENT_RE = re.compile(r"tooltip|requirement|required|trigger|valid|allowed|cannot|can_|unlock|available|need|must", re.I)
EFFECT_RE = re.compile(r"effect|modifier|gain|loss|\\n|\n|\$EFFECT_LIST_BULLET\$|#weak|#bold|#indent", re.I)
EVENT_RE = re.compile(r"event|\.desc|desc\.|option|toast|dialogue|story|scheme|interaction|memory|activity", re.I)
CULTURE_RE = re.compile(r"culture|cultural|tradition|heritage|ethos|language", re.I)
RELIGION_RE = re.compile(r"religion|faith|doctrine|tenet|holy_site|holy site|temple|church", re.I)
NAME_RE = re.compile(r"_name\b|name_|nickname|dynasty|house|GetName|GetDynasty|epithet", re.I)
TITLE_LAW_RE = re.compile(r"title|law|government|realm|succession|county|duchy|kingdom|empire|vassal|liege|rank|holding", re.I)
DYNAMIC_RE = re.compile(r"Custom\(|Select_CString|Concept\(|ScriptValue|GetTrait|ROOT\.|FROM\.|SCOPE\.|TARGET\.|\[[^\]]+\]|\$[^$]+\$", re.I)
GENDER_RE = re.compile(r"Select_CString|ES_(?:OA|XA|EA|ElLa|DelDela|AlAla|A|O)|Get(?:SheHe|HerHis|HerHim)|local_player|GetPlayer|GetLocalPlayer", re.I)
SCRIPT_VALUE_RE = re.compile(r"ScriptValue|GetScriptValue|\|V[0-9]?|\|=\+?0|[0-9]+%", re.I)
ACCOLADE_RE = re.compile(r"accolade|acclaimed_knight|knight|trait|GetTrait|prowess", re.I)
BUILDING_RE = re.compile(r"building|modifier|holding|construct|duchy_building", re.I)
ARTIFACT_RE = re.compile(r"artifact|activity|travel|tournament|legend|hunt|feast|wedding", re.I)
SCOPE_RE = re.compile(r"ROOT\.|FROM\.|SCOPE\.|TARGET\.|CHARACTER\.|Get[A-Za-z0-9_]+\(", re.I)
RESIDUAL_RE = re.compile(r"NÃ|Ãƒ|Ã‚|ï¿½|\b(the|your|you|their|cannot|sera|será|mas|más|facil|fácil)\b", re.I)


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_domain_context_after_requirement_effect_review"
    spec = reports_dir / f"{stamp}_domain_context_after_requirement_effect_spec.json"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), spec


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


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


def registry_metrics(conn: sqlite3.Connection) -> dict[str, int]:
    registry = agent_inventory.fetch_registry(conn)
    latest_run, routing = agent_inventory.fetch_latest_routing(conn)
    recommendations = agent_inventory.fetch_recommendations(conn)
    evidence, _table_counts = agent_inventory.fetch_agent_evidence(conn)
    rows = agent_inventory.build_rows(registry, routing, evidence, recommendations)
    metrics = {
        "registered_agents": len(registry),
        "observed_agent_keys": len(rows),
        "operational_agents": sum(1 for row in registry if row.get("operational_state") == "operational"),
        "dry_run_agents": sum(1 for row in registry if row.get("operational_state") == "dry_run"),
        "shadow_agents": sum(1 for row in registry if row.get("operational_state") == "shadow"),
        "latest_routing_run_id": int(latest_run["id"]) if latest_run else 0,
    }
    expected = {
        "registered_agents": EXPECTED_REGISTERED_AGENTS,
        "observed_agent_keys": EXPECTED_OBSERVED_AGENT_KEYS,
        "operational_agents": EXPECTED_OPERATIONAL_AGENTS,
        "dry_run_agents": EXPECTED_DRY_RUN_AGENTS,
        "shadow_agents": EXPECTED_SHADOW_AGENTS,
    }
    for key, value in expected.items():
        if metrics[key] != value:
            raise SystemExit(f"registry guard failed: {key}={metrics[key]} expected {value}")
    return metrics


def text_rows(conn: sqlite3.Connection, segment_ids: list[int], run_id: int) -> dict[int, dict[str, Any]]:
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT
            s.segment_id,
            s.relative_path,
            s.source_key,
            s.state_group,
            s.is_closed,
            s.needs_output_apply,
            s.confirmed_matches_output,
            src.old_text,
            src.spanish_text,
            src.english_text,
            out.portuguese_text AS output_text
        FROM segment_state_items s
        LEFT JOIN source_segments src
          ON src.id = s.segment_id
        LEFT JOIN output_segments out
          ON out.segment_id = s.segment_id
        WHERE s.run_id = ?
          AND s.segment_id IN ({placeholders})
        """,
        (run_id, *segment_ids),
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def has(pattern: re.Pattern[str], blob: str, label: str) -> list[str]:
    return [label] if pattern.search(blob) else []


def marker_groups(blob: str) -> dict[str, list[str]]:
    return {
        "domain_markers": has(DOMAIN_RE, blob, "Domain"),
        "requirement_markers": has(REQUIREMENT_RE, blob, "Requirement"),
        "effect_markers": has(EFFECT_RE, blob, "Effect"),
        "event_markers": has(EVENT_RE, blob, "Event"),
        "culture_markers": has(CULTURE_RE, blob, "Culture"),
        "religion_markers": has(RELIGION_RE, blob, "Religion"),
        "name_markers": has(NAME_RE, blob, "Name"),
        "title_law_markers": has(TITLE_LAW_RE, blob, "TitleLaw"),
        "dynamic_markers": has(DYNAMIC_RE, blob, "DynamicToken"),
        "guard_markers": ["StateClean", "NoOutputApply", "ConfirmedMatchesOutput"],
        "secondary_markers": [
            label
            for pattern, label in [
                (GENDER_RE, "GenderLocalPlayer"),
                (SCRIPT_VALUE_RE, "ScriptValue"),
                (ACCOLADE_RE, "AccoladeTrait"),
                (BUILDING_RE, "BuildingModifier"),
                (ARTIFACT_RE, "ArtifactActivity"),
                (SCOPE_RE, "ScopeGetter"),
                (RESIDUAL_RE, "ResidualVisible"),
            ]
            if pattern.search(blob)
        ],
    }


def decide(row: dict[str, Any], blob: str, markers: dict[str, list[str]]) -> tuple[str, str, str, str, str]:
    families = set(row.get("families_open") or [])
    if RESIDUAL_RE.search(blob) or "spanish_residual_microagent" in families:
        return (
            "domain_context_reuse_requirement_effect_residual_policy",
            "residual_repair_after_requirement_effect",
            "",
            "residual_repair_after_requirement_effect",
            "visible residual marker should reuse registered requirement/effect residual splitter",
        )
    if RELIGION_RE.search(blob) and re.search(r"holy_site|holy site", blob, re.I):
        return (
            "domain_context_reuse_holy_site_effect_name_policy",
            "holy_site_effect_name_policy",
            "",
            "holy_site_effect_name_policy",
            "holy-site/religion domain can reuse the registered holy-site effect-name splitter",
        )
    if BUILDING_RE.search(blob):
        return (
            "domain_context_reuse_building_modifier_effect_policy",
            "building_modifier_effect_policy",
            "",
            "building_modifier_effect_policy",
            "building/modifier marker can reuse existing requirement/effect splitter",
        )
    if EVENT_RE.search(blob):
        return (
            "domain_context_reuse_requirement_effect_event_context_policy",
            "event_context_after_requirement_effect",
            "",
            "event_context_after_requirement_effect",
            "event/context marker can reuse existing requirement/effect event-context splitter",
        )
    if GENDER_RE.search(blob):
        return (
            "domain_context_reuse_effect_list_gender_local_player_policy",
            "effect_list_gender_local_player_policy",
            "",
            "effect_list_gender_local_player_policy",
            "gender/local-player marker can reuse registered effect-list gender policy",
        )
    if ACCOLADE_RE.search(blob):
        return (
            "domain_context_reuse_effect_list_trait_accolade_policy",
            "effect_list_trait_accolade_policy",
            "",
            "effect_list_trait_accolade_policy",
            "trait/accolade marker can reuse registered effect-list trait/accolade policy",
        )
    if CULTURE_RE.search(blob):
        return (
            "domain_context_reuse_not_requirement_effect_culture_policy",
            "not_requirement_effect_culture_policy",
            "",
            "not_requirement_effect_culture_policy",
            "culture domain marker can reuse the registered not_requirement_effect culture subrouter",
        )
    if RELIGION_RE.search(blob):
        return (
            "needs_domain_religion_holy_site_policy",
            "",
            "",
            "domain_religion_holy_site_policy",
            "religion/faith domain remains without a specific registered domain-context policy",
        )
    if NAME_RE.search(blob):
        return (
            "needs_domain_culture_name_policy",
            "",
            "",
            "domain_culture_name_policy",
            "name/dynasty/location domain remains after existing reuses",
        )
    if TITLE_LAW_RE.search(blob):
        return (
            "needs_domain_title_law_policy",
            "",
            "",
            "domain_title_law_policy",
            "title/law/government marker is the clearest remaining domain sublane",
        )
    if SCRIPT_VALUE_RE.search(blob):
        return (
            "needs_domain_script_value_policy",
            "",
            "",
            "domain_script_value_policy",
            "ScriptValue marker remains after existing reuses",
        )
    if ARTIFACT_RE.search(blob):
        return (
            "needs_domain_artifact_activity_policy",
            "",
            "",
            "domain_artifact_activity_policy",
            "artifact/activity marker remains after existing reuses",
        )
    if SCOPE_RE.search(blob):
        return (
            "needs_domain_scope_getter_policy",
            "",
            "",
            "domain_scope_getter_policy",
            "scope/getter marker remains after domain guards",
        )
    if DYNAMIC_RE.search(blob):
        return (
            "needs_domain_dynamic_parser_escape",
            "",
            "",
            "ck3_dynamic_expression_parser_spec",
            "dynamic token should escape to parser after domain-context checks",
        )
    if EFFECT_RE.search(blob):
        return (
            "domain_context_reuse_effect_list_concept_policy",
            "effect_list_concept_policy",
            "",
            "effect_list_concept_policy",
            "effect/concept-like domain can reuse effect-list concept policy",
        )
    return (
        "domain_context_terminal_policy_with_requirement_guard",
        "",
        "domain_context_after_requirement_effect",
        "domain_context_after_requirement_effect",
        "domain-context surface appears terminal/read-only with requirement guard",
    )


def make_sample(record: dict[str, Any], text: dict[str, Any]) -> dict[str, Any]:
    old_text = str(text.get("old_text") or "")
    output_text = str(text.get("output_text") or "")
    blob = " ".join([
        str(text.get("relative_path") or record["relative_path"]),
        str(text.get("source_key") or record["source_key"]),
        old_text,
        str(text.get("spanish_text") or ""),
        str(text.get("english_text") or ""),
        output_text,
        " ".join(record.get("families_open") or []),
    ])
    markers = marker_groups(blob)
    decision, registered, catalog, next_component, rationale = decide(record, blob, markers)
    return {
        "record_type": "sample_review",
        "segment_id": int(record["segment_id"]),
        "relative_path": str(text.get("relative_path") or record["relative_path"]),
        "source_key": str(text.get("source_key") or record["source_key"]),
        "families_open": record.get("families_open") or [],
        "primary_route": EXPECTED_ROUTE,
        "old_text": old_text,
        "confirmed_text": output_text,
        "output_text": output_text,
        **markers,
        "matched_registered_policy": registered,
        "matched_catalog_spec": catalog,
        "domain_context_decision": decision,
        "next_component": next_component,
        "requires_lifecycle_later": False,
        "requires_apply_later": False,
        "corrected_text": "",
        "rationale": rationale,
    }


def write_outputs(
    *,
    args: argparse.Namespace,
    state: dict[str, int],
    registry: dict[str, int],
    universe: int,
    samples: list[dict[str, Any]],
) -> tuple[Path, Path, Path]:
    txt_path, jsonl_path, spec_path = output_paths()
    decision_counts = Counter(row["domain_context_decision"] for row in samples)
    reuse_count = sum(1 for row in samples if row["domain_context_decision"].startswith("domain_context_reuse_"))
    terminal_count = sum(1 for row in samples if row["domain_context_decision"].startswith("domain_context_terminal_policy"))
    needs_counts = Counter(row["domain_context_decision"] for row in samples if row["domain_context_decision"].startswith("needs_"))
    dominant_decision, dominant_count = decision_counts.most_common(1)[0]
    next_prompt = "chat_exec_domain_context_after_requirement_effect_catalog_registration_prompt.md"
    concentrated_need = next(((key, count) for key, count in needs_counts.most_common() if count >= 40), None)
    if concentrated_need:
        slug = concentrated_need[0].replace("needs_domain_", "")
        next_prompt = f"chat_exec_domain_context_after_requirement_effect_{slug}_review_prompt.md"
    elif reuse_count >= 50:
        next_prompt = "chat_exec_domain_context_after_requirement_effect_catalog_registration_prompt.md"
    elif terminal_count >= 50:
        next_prompt = "chat_exec_domain_context_after_requirement_effect_terminal_registration_prompt.md"
    else:
        next_prompt = "chat_exec_blocked_uncertain_review_prompt.md"
    marker_fields = [
        "domain_markers",
        "requirement_markers",
        "effect_markers",
        "event_markers",
        "culture_markers",
        "religion_markers",
        "name_markers",
        "title_law_markers",
        "dynamic_markers",
        "guard_markers",
        "secondary_markers",
    ]
    marker_counts = {
        field: dict(Counter(marker for row in samples for marker in row[field]).most_common(20))
        for field in marker_fields
    }
    family_counts = Counter(family for row in samples for family in row["families_open"])
    summary = {
        "record_type": "summary",
        "source": SOURCE,
        "segment_state_run_id": args.segment_state_run_id,
        "ledger_run_id": args.ledger_run_id,
        **state,
        **registry,
        "universe_estimated": universe,
        "total_reviewed": len(samples),
        "reuse_registered_or_cataloged_count": reuse_count,
        "terminal_policy_count": terminal_count,
        "ready_lifecycle_future": 0,
        "apply_candidates_future": 0,
        "requires_lifecycle_later": False,
        "requires_apply_later": False,
        "decision_counts": dict(decision_counts),
        "dominant_subtype": dominant_decision,
        "dominant_count": dominant_count,
        "package_assessment": "reuse_splitter_component" if reuse_count >= 50 else ("terminal_component" if terminal_count >= 50 else "fragmented_context_queue"),
        "next_prompt": next_prompt,
    }
    spec = {
        "schema_version": 1,
        "created_for": "read_only_subpolicy_design",
        "parent_policy": "requirement_effect_router_readonly",
        "policy_id": EXPECTED_ROUTE,
        "segment_state_run_id": args.segment_state_run_id,
        "ledger_run_id": args.ledger_run_id,
        "entry_conditions": [
            "route == domain_context_after_requirement_effect",
            "segment remains pending",
            "needs_output_apply == 0",
            "confirmed_matches_output == 1",
        ],
        "reused_registered_policies": [
            {"agent_key": key, "sampled": sum(1 for row in samples if row["matched_registered_policy"] == key)}
            for key in sorted({row["matched_registered_policy"] for row in samples if row["matched_registered_policy"]})
        ],
        "reused_catalog_specs": [
            {"policy_id": key, "sampled": sum(1 for row in samples if row["matched_catalog_spec"] == key)}
            for key in sorted({row["matched_catalog_spec"] for row in samples if row["matched_catalog_spec"]})
        ],
        "domain_context_types": [{"type": key, "sampled": value} for key, value in decision_counts.most_common()],
        "resolution_order": [
            "state guard",
            "residual/holy-site/building/event registered reuse",
            "gender/accolade/culture registered reuse",
            "religion/name/title/ScriptValue/artifact/scope subpolicy split",
            "dynamic parser escape",
            "terminal requirement/domain guard",
        ],
        "next_components": [next_prompt],
        "blocked_conditions": [
            "state guard failed",
            "needs_output_apply != 0",
            "confirmed_matches_output != 1",
            "ambiguous domain-context evidence",
        ],
        "promotion_gate": "read_only_component_only_no_apply_no_lifecycle",
        "observed_decision_counts": dict(decision_counts),
    }
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(summary, ensure_ascii=False, sort_keys=True) + "\n")
        for field, counts in marker_counts.items():
            for marker, count in counts.items():
                handle.write(json.dumps({"record_type": f"top_{field}", "value": marker, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for family, count in family_counts.most_common(20):
            handle.write(json.dumps({"record_type": "top_family", "family": family, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for sample in samples:
            handle.write(json.dumps(sample, ensure_ascii=False, sort_keys=True) + "\n")
    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("Domain context after requirement/effect review\n\n")
        for key in [
            "universe_estimated",
            "total_reviewed",
            "reuse_registered_or_cataloged_count",
            "terminal_policy_count",
            "ready_lifecycle_future",
            "apply_candidates_future",
            "dominant_subtype",
            "dominant_count",
            "package_assessment",
            "next_prompt",
        ]:
            handle.write(f"- {key}: {summary[key]}\n")
        handle.write("\nDecisoes\n")
        for decision, count in decision_counts.most_common():
            handle.write(f"- {decision}: {count}\n")
        handle.write("\nTop families\n")
        for family, count in family_counts.most_common(12):
            handle.write(f"- {family}: {count}\n")
        handle.write("\nRespostas objetivas\n")
        handle.write(f"- Deve virar componente read-only real: {'sim' if reuse_count >= 50 or terminal_count >= 50 else 'ainda nao; precisa split/triage'}.\n")
        handle.write("- Nao gera lifecycle/apply em curto prazo.\n")
        handle.write(f"- Reuso de policies/specs catalogadas: {reuse_count}/{len(samples)}.\n")
        handle.write(f"- Proximo prompt recomendado: {next_prompt}.\n")
    return txt_path, jsonl_path, spec_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Review domain_context_after_requirement_effect read-only.")
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    parser.add_argument("--ledger-run-id", required=True, type=int)
    parser.add_argument("--limit", type=int, default=200)
    args = parser.parse_args()
    if args.segment_state_run_id != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("segment_state_run_id guard failed")
    if args.ledger_run_id != EXPECTED_LEDGER_RUN_ID:
        raise SystemExit("ledger_run_id guard failed")
    if args.limit > 200:
        raise SystemExit("limit guard failed: max 200")
    with connect_readonly() as conn:
        state = state_counts(conn, args.segment_state_run_id)
        registry = registry_metrics(conn)
        router.fetch_runs(conn, args.segment_state_run_id, args.ledger_run_id)
        grouped = router.fetch_pending_rows(conn, args.segment_state_run_id, args.ledger_run_id)
        route_records: list[dict[str, Any]] = []
        for segment_id, rows in grouped.items():
            blob = router.blob_for(rows)
            markers = router.detect_markers(blob)
            route, _reason = router.route_for(blob, markers)
            if route != EXPECTED_ROUTE:
                continue
            first = rows[0]
            route_records.append({
                "segment_id": int(segment_id),
                "relative_path": str(first.get("relative_path") or ""),
                "source_key": str(first.get("source_key") or ""),
                "families_open": list(router.families_for(rows)),
            })
        universe = len(route_records)
        if universe != EXPECTED_UNIVERSE:
            raise SystemExit(f"domain_context universe guard failed: {universe} expected {EXPECTED_UNIVERSE}")
        selected = route_records[: args.limit]
        text_by_id = text_rows(conn, [int(row["segment_id"]) for row in selected], args.segment_state_run_id)
    samples: list[dict[str, Any]] = []
    seen: set[int] = set()
    for record in selected:
        segment_id = int(record["segment_id"])
        if segment_id in seen:
            raise SystemExit(f"duplicate sampled segment_id: {segment_id}")
        seen.add(segment_id)
        text = text_by_id.get(segment_id)
        if not text:
            raise SystemExit(f"missing state/text row for segment_id={segment_id}")
        if str(text.get("state_group") or "") != "pending" or int(text.get("is_closed") or 0) != 0:
            raise SystemExit(f"pending guard failed for segment_id={segment_id}")
        if int(text.get("needs_output_apply") or 0) != 0:
            raise SystemExit(f"needs_output_apply guard failed for segment_id={segment_id}")
        if int(text.get("confirmed_matches_output") or 0) != 1:
            raise SystemExit(f"confirmed_matches_output guard failed for segment_id={segment_id}")
        samples.append(make_sample(record, text))
    if any(row["requires_apply_later"] for row in samples):
        raise SystemExit("requires_apply_later guard failed")
    if any(row["requires_lifecycle_later"] for row in samples):
        raise SystemExit("requires_lifecycle_later guard failed")
    txt_path, jsonl_path, spec_path = write_outputs(
        args=args,
        state=state,
        registry=registry,
        universe=universe,
        samples=samples,
    )
    print(f"txt: {txt_path}")
    print(f"jsonl: {jsonl_path}")
    print(f"spec: {spec_path}")
    print(f"universe_estimated: {universe}")
    print(f"total_reviewed: {len(samples)}")


if __name__ == "__main__":
    main()
