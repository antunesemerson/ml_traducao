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
import macro_lane_router_architecture_review as macro_router
import requirement_effect_router_readonly as req_router


SOURCE = "not_requirement_effect_global_router_review_v1"
EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76
EXPECTED_CLOSED_COUNT = 276375
EXPECTED_PENDING_COUNT = 11725
EXPECTED_OUTPUT_APPLY_PENDING_COUNT = 0
EXPECTED_REGISTERED_AGENTS = 225
EXPECTED_OBSERVED_AGENT_KEYS = 282
EXPECTED_OPERATIONAL_AGENTS = 33
EXPECTED_SHADOW_AGENTS = 84
EXPECTED_UNIVERSE = 1248


DYNAMIC_RE = re.compile(r"Custom\(|SelectLocalization|Concept\(|ScriptValue|GetTrait|ROOT\.|FROM\.|SCOPE\.|TARGET\.|\[[^\]]+\]|\$[^$]+\$", re.I)
SELECT_RE = re.compile(r"Select_CString", re.I)
GENDER_RE = re.compile(r"Select_CString|ES_(?:OA|XA|EA|ElLa|DelDela|AlAla|A|O)|Get(?:SheHe|HerHis|HerHim|WomanMan|WomenMen)|local_player|GetPlayer|GetLocalPlayer|\bvoc(?:ê|Ãª|ÃƒÂª)\b|\bseu\b|\bsua\b", re.I)
SHORT_LABEL_RE = re.compile(r"\b(short_label|label|name|title|nickname|effect_name)\b", re.I)
SEMANTIC_RE = re.compile(r"\bsemantic|context|meaning|domain\b", re.I)
AUTOFIX_RE = re.compile(r"autofix|unknown|false reopen|needs_reopen", re.I)
CULTURE_RELIGION_RE = re.compile(r"culture|religion|faith|doctrine|holy_site|tradition|heritage|language", re.I)
NAME_DYNASTY_RE = re.compile(r"dynasty|house|nickname|epithet|GetName|GetDynasty|_name\b|name_", re.I)
DOMAIN_EVENT_RE = re.compile(r"event|\.desc|desc\.|option|toast|dialogue|story|scheme|interaction|memory|activity|court|war|travel", re.I)
ACTOR_TARGET_RE = re.compile(r"\b(actor|target|recipient|root|from|scope|ROOT|FROM|TARGET|SCOPE|CHARACTER)\b", re.I)
RESIDUAL_RE = re.compile(r"NÃ|Ãƒ|Ã‚|ï¿½|\b(the|your|you|their|cannot|consiguio|consiguió|sera|será|mas|más|facil|fácil)\b", re.I)


REGISTERED_POLICY_BY_DECISION = {
    "not_req_reuse_gender_local_player_policy": "gender_local_player_policy",
    "not_req_reuse_semantic_review_router": "semantic_review_router",
    "not_req_reuse_requirement_effect_router": "requirement_effect_router_readonly",
    "not_req_reuse_effect_list_policy": "effect_list_multiline_policy",
    "not_req_reuse_script_value_effect_policy": "script_value_effect_policy",
    "not_req_reuse_holy_site_effect_name_policy": "holy_site_effect_name_policy",
}

CATALOG_SPEC_BY_DECISION = {
    "not_req_reuse_macro_lane_router": "macro_lane_router",
    "not_req_reuse_dynamic_parser_policy": "ck3_dynamic_expression_parser_spec",
    "not_req_reuse_select_cstring_policy": "select_cstring_player_target_direct_policy",
    "not_req_reuse_short_label_style_policy": "short_label_style_policy",
    "not_req_reuse_autofix_unknown_router": "autofix_unknown_router",
    "not_req_reuse_culture_semantic_policy": "culture_semantic_policy",
    "not_req_reuse_name_dynasty_policy": "name_dynasty_policy",
    "not_req_reuse_domain_context_policy": "domain_context_policy",
    "not_req_reuse_event_context_policy": "event_context_policy",
}


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_not_requirement_effect_global_router_review"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), base.with_name(base.name.replace("_review", "_spec") + ".json")


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    probe = conn.execute("PRAGMA query_only").fetchone()
    if int(probe[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


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
    metrics = {
        "registered_agents": len(registry),
        "observed_agent_keys": len(rows),
        "operational_agents": sum(1 for row in registry if row.get("operational_state") == "operational"),
        "shadow_agents": sum(1 for row in registry if row.get("operational_state") == "shadow"),
        "latest_routing_run_id": int(latest_run["id"]) if latest_run else 0,
    }
    expected = {
        "registered_agents": EXPECTED_REGISTERED_AGENTS,
        "observed_agent_keys": EXPECTED_OBSERVED_AGENT_KEYS,
        "operational_agents": EXPECTED_OPERATIONAL_AGENTS,
        "shadow_agents": EXPECTED_SHADOW_AGENTS,
    }
    for key, value in expected.items():
        if metrics[key] != value:
            raise SystemExit(f"registry guard failed: {key}={metrics[key]} expected {value}")
    return metrics


def text_rows(conn: sqlite3.Connection, segment_ids: list[int], run_id: int) -> dict[int, dict[str, Any]]:
    if not segment_ids:
        return {}
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
            s.needs_reopen,
            s.priority_score,
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


def markers(pattern: re.Pattern[str], blob: str, labels: list[str]) -> list[str]:
    return labels if pattern.search(blob) else []


def list_markers(blob: str, families: list[str]) -> dict[str, list[str]]:
    return {
        "dynamic_markers": markers(DYNAMIC_RE, blob, ["DynamicToken"]),
        "gender_local_player_markers": markers(GENDER_RE, blob, ["GenderLocalPlayer"]),
        "short_label_markers": ["ShortLabel"] if "short_label_style_microagent" in families or SHORT_LABEL_RE.search(blob) else [],
        "semantic_markers": ["Semantic"] if "semantic_review_router" in families or SEMANTIC_RE.search(blob) else [],
        "autofix_markers": ["AutofixUnknown"] if "autofix_unknown_microagent" in families or AUTOFIX_RE.search(blob) else [],
        "culture_religion_markers": markers(CULTURE_RELIGION_RE, blob, ["CultureReligion"]),
        "name_dynasty_markers": markers(NAME_DYNASTY_RE, blob, ["NameDynasty"]),
        "domain_event_markers": markers(DOMAIN_EVENT_RE, blob, ["DomainEvent"]),
        "guard_markers": ["StateClean", "NoOutputApply", "ConfirmedMatchesOutput"],
        "secondary_markers": markers(ACTOR_TARGET_RE, blob, ["ActorTargetScope"]),
    }


def decide(record: dict[str, Any], blob: str, lane_ids: list[str], marker_groups: dict[str, list[str]]) -> tuple[str, str, str, str, str]:
    families = set(record["families_open"])
    source_key = str(record["source_key"])
    if SELECT_RE.search(blob):
        return (
            "not_req_reuse_select_cstring_policy",
            "",
            "select_cstring_player_target_direct_policy",
            "gender_local_player_policy",
            "Select_CString surface can reuse existing select-cstring/local-player specs",
        )
    if marker_groups["gender_local_player_markers"] or "gender_token_microagent" in families:
        return (
            "not_req_reuse_gender_local_player_policy",
            "gender_local_player_policy",
            "",
            "gender_local_player_policy",
            "gender/local-player marker or family is present outside requirement/effect",
        )
    if DYNAMIC_RE.search(blob) or "02_dynamic_parser" in lane_ids:
        return (
            "not_req_reuse_dynamic_parser_policy",
            "",
            "ck3_dynamic_expression_parser_spec",
            "ck3_dynamic_expression_parser_spec",
            "dynamic CK3 token/scope/concept marker is the first reusable parser step",
        )
    if "short_label_style_microagent" in families:
        return (
            "not_req_reuse_short_label_style_policy",
            "",
            "short_label_style_policy",
            "short_label_style_policy",
            "short-label family dominates this non requirement/effect item",
        )
    if "semantic_review_router" in families:
        return (
            "not_req_reuse_semantic_review_router",
            "semantic_review_router",
            "",
            "semantic_review_router",
            "semantic review family is the clearest existing router",
        )
    if "autofix_unknown_microagent" in families:
        return (
            "not_req_reuse_autofix_unknown_router",
            "",
            "autofix_unknown_router",
            "autofix_unknown_router",
            "autofix_unknown family should reuse the existing unknown router lane",
        )
    if CULTURE_RELIGION_RE.search(blob) or "culture_semantic_microagent" in families or "religion_semantic_microagent" in families:
        return (
            "needs_not_req_culture_religion_router",
            "",
            "",
            "not_req_culture_religion_router",
            "culture/religion surface appears outside requirement/effect and needs a global router",
        )
    if NAME_DYNASTY_RE.search(blob) or "nickname_name_policy" in families or "_name" in source_key:
        return (
            "needs_not_req_name_dynasty_router",
            "",
            "",
            "not_req_name_dynasty_router",
            "name/dynasty/nickname signal is present but not covered by requirement/effect",
        )
    if DOMAIN_EVENT_RE.search(blob) or "09_event_context" in lane_ids:
        return (
            "needs_not_req_event_context_router",
            "",
            "",
            "not_req_event_context_router",
            "event/domain context appears outside requirement/effect and needs a global context router",
        )
    if ACTOR_TARGET_RE.search(blob):
        return (
            "needs_not_req_actor_target_router",
            "",
            "",
            "not_req_actor_target_router",
            "actor/target/scope perspective remains after higher-priority lanes",
        )
    if RESIDUAL_RE.search(blob):
        return (
            "needs_not_req_residual_repair",
            "",
            "",
            "not_req_residual_repair",
            "visible residual marker remains but no apply is proposed in this review",
        )
    return (
        "not_req_reuse_macro_lane_router",
        "",
        "macro_lane_router",
        "macro_lane_router",
        "no specific downstream router dominates, so macro-lane router remains the right first layer",
    )


def make_sample(
    record: dict[str, Any],
    text: dict[str, Any],
    macro_grouped_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    lane_pairs = macro_router.candidate_lanes(macro_grouped_rows)
    lane_ids = [lane for lane, _reason in lane_pairs]
    lane_labels = [macro_router.LANE_BY_ID[lane]["label"] for lane in lane_ids]
    families = list(record["families_open"])
    old_text = str(text.get("old_text") or "")
    output_text = str(text.get("output_text") or "")
    blob = " ".join([
        str(text.get("relative_path") or ""),
        str(text.get("source_key") or ""),
        old_text,
        str(text.get("spanish_text") or ""),
        str(text.get("english_text") or ""),
        output_text,
        " ".join(families),
    ])
    marker_groups = list_markers(blob, families)
    decision, matched_registered, matched_catalog, next_component, rationale = decide(record, blob, lane_ids, marker_groups)
    return {
        "record_type": "sample_review",
        "segment_id": int(record["segment_id"]),
        "relative_path": str(text.get("relative_path") or record["relative_path"]),
        "source_key": str(text.get("source_key") or record["source_key"]),
        "families_open": families,
        "primary_gap": "not_requirement_effect",
        "old_text": old_text,
        "confirmed_text": output_text,
        "output_text": output_text,
        "macro_lane_candidates": lane_labels,
        **marker_groups,
        "matched_registered_policy": matched_registered,
        "matched_catalog_spec": matched_catalog,
        "not_requirement_decision": decision,
        "recommended_first_component": next_component,
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
    route_counts: Counter[str],
) -> tuple[Path, Path, Path]:
    txt_path, jsonl_path, spec_path = output_paths()
    decision_counts = Counter(str(row["not_requirement_decision"]) for row in samples)
    macro_counts = Counter(label for row in samples for label in row["macro_lane_candidates"])
    reuse_count = sum(1 for row in samples if str(row["not_requirement_decision"]).startswith("not_req_reuse_"))
    needs_router_count = sum(1 for row in samples if str(row["not_requirement_decision"]).startswith("needs_not_req_"))
    dominant_decision, dominant_count = decision_counts.most_common(1)[0]
    dominant_macro, dominant_macro_count = macro_counts.most_common(1)[0]
    next_prompt = "chat_exec_not_requirement_effect_global_router_registration_prompt.md"
    concentrated_need = next(
        ((decision, count) for decision, count in decision_counts.most_common() if decision.startswith("needs_not_req_") and count >= 50),
        None,
    )
    if concentrated_need:
        slug = concentrated_need[0].replace("needs_not_req_", "").replace("_router", "")
        next_prompt = f"chat_exec_not_requirement_effect_{slug}_review_prompt.md"
    elif reuse_count >= 70:
        next_prompt = "chat_exec_not_requirement_effect_global_router_registration_prompt.md"
    elif dominant_macro_count >= 70:
        next_prompt = "chat_exec_not_requirement_effect_macro_lane_cohort_validation_prompt.md"
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
        "needs_new_router_count": needs_router_count,
        "ready_lifecycle_future": 0,
        "apply_candidates_future": 0,
        "requires_lifecycle_later": False,
        "requires_apply_later": False,
        "decision_counts": dict(decision_counts),
        "top_macro_lane_candidates": dict(macro_counts.most_common(12)),
        "dominant_subtype": dominant_decision,
        "dominant_count": dominant_count,
        "dominant_macro_lane": dominant_macro,
        "dominant_macro_lane_count": dominant_macro_count,
        "package_assessment": "reuse_splitter_router" if reuse_count >= 70 else "fragmented_macro_lane_mix",
        "network_should_show_as_external_backlog": True,
        "next_prompt": next_prompt,
    }
    marker_fields = [
        "dynamic_markers",
        "gender_local_player_markers",
        "short_label_markers",
        "semantic_markers",
        "autofix_markers",
        "culture_religion_markers",
        "name_dynasty_markers",
        "domain_event_markers",
        "guard_markers",
        "secondary_markers",
    ]
    marker_counts = {
        field: dict(Counter(marker for row in samples for marker in row[field]).most_common(20))
        for field in marker_fields
    }
    family_counts = Counter(family for row in samples for family in row["families_open"])
    spec = {
        "schema_version": 1,
        "created_for": "read_only_global_router_design",
        "parent_policy": "macro_lane_router",
        "policy_id": "not_requirement_effect_global_router",
        "segment_state_run_id": args.segment_state_run_id,
        "ledger_run_id": args.ledger_run_id,
        "entry_conditions": [
            "route == not_requirement_effect",
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
        "macro_lane_routes": [{"lane": key, "sampled": value} for key, value in macro_counts.most_common()],
        "resolution_order": [
            "state guard",
            "Select_CString/gender-local-player reuse",
            "dynamic parser reuse",
            "short-label/semantic/autofix reuse",
            "culture/religion/name/domain router decisions",
            "residual guard",
            "macro-lane fallback",
        ],
        "next_components": [summary["next_prompt"]],
        "blocked_conditions": [
            "state guard failed",
            "needs_output_apply != 0",
            "confirmed_matches_output != 1",
            "insufficient macro-lane concentration",
        ],
        "promotion_gate": "read_only_router_only_no_apply_no_lifecycle",
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
        for route, count in route_counts.most_common(20):
            handle.write(json.dumps({"record_type": "route_count", "route": route, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for sample in samples:
            handle.write(json.dumps(sample, ensure_ascii=False, sort_keys=True) + "\n")
    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("Not requirement/effect global router review\n\n")
        for key in [
            "universe_estimated",
            "total_reviewed",
            "reuse_registered_or_cataloged_count",
            "needs_new_router_count",
            "ready_lifecycle_future",
            "apply_candidates_future",
            "dominant_subtype",
            "dominant_count",
            "dominant_macro_lane",
            "dominant_macro_lane_count",
            "package_assessment",
            "next_prompt",
        ]:
            handle.write(f"- {key}: {summary[key]}\n")
        handle.write("\nDecisoes\n")
        for decision, count in decision_counts.most_common():
            handle.write(f"- {decision}: {count}\n")
        handle.write("\nMacro-lanes\n")
        for lane, count in macro_counts.most_common(12):
            handle.write(f"- {lane}: {count}\n")
        handle.write("\nTop families\n")
        for family, count in family_counts.most_common(12):
            handle.write(f"- {family}: {count}\n")
        handle.write("\nRespostas objetivas\n")
        handle.write(f"- not_requirement_effect e {'um pacote de reuso/splitter' if reuse_count >= 70 else 'uma mistura de macro-lanes'}.\n")
        handle.write("- Deve virar componente read-only real se o registro posterior mantiver flags de apply/lifecycle zeradas.\n")
        handle.write(f"- Primeiro componente recomendado: {summary['next_prompt']}.\n")
        handle.write(f"- Reaproveitamento de componentes/specs existentes: {reuse_count}/{len(samples)}.\n")
        handle.write("- Network deve mostrar este bloco como backlog externo ao requirement/effect, ligado ao macro-lane router.\n")
        handle.write("- Producao/lifecycle/apply nao sao recomendados.\n")
    return txt_path, jsonl_path, spec_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Review not_requirement_effect as a read-only global router candidate.")
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    parser.add_argument("--ledger-run-id", required=True, type=int)
    parser.add_argument("--limit", type=int, default=240)
    args = parser.parse_args()
    if args.segment_state_run_id != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("segment_state_run_id guard failed")
    if args.ledger_run_id != EXPECTED_LEDGER_RUN_ID:
        raise SystemExit("ledger_run_id guard failed")
    if args.limit > 240:
        raise SystemExit("limit guard failed: max 240")

    with connect_readonly() as conn:
        state = state_counts(conn, args.segment_state_run_id)
        registry = registry_metrics(conn)
        req_router.fetch_runs(conn, args.segment_state_run_id, args.ledger_run_id)
        req_grouped = req_router.fetch_pending_rows(conn, args.segment_state_run_id, args.ledger_run_id)
        macro_rows = macro_router.fetch_pending_rows(conn, args.segment_state_run_id, args.ledger_run_id)
        macro_grouped = macro_router.group_by_segment(macro_rows)
        route_counts: Counter[str] = Counter()
        not_req_records: list[dict[str, Any]] = []
        for segment_id, rows in req_grouped.items():
            blob = req_router.blob_for(rows)
            req_markers = req_router.detect_markers(blob)
            route, _reason = req_router.route_for(blob, req_markers)
            route_counts[route] += 1
            if route != "not_requirement_effect":
                continue
            families = list(req_router.families_for(rows))
            first = rows[0]
            not_req_records.append({
                "segment_id": segment_id,
                "relative_path": str(first.get("relative_path") or ""),
                "source_key": str(first.get("source_key") or ""),
                "families_open": families,
            })
        universe = len(not_req_records)
        if universe != EXPECTED_UNIVERSE:
            raise SystemExit(f"not_requirement_effect universe guard failed: {universe} expected {EXPECTED_UNIVERSE}")
        selected = not_req_records[: args.limit]
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
            raise SystemExit(f"missing text/state row for segment_id={segment_id}")
        if str(text.get("state_group") or "") != "pending" or int(text.get("is_closed") or 0) != 0:
            raise SystemExit(f"pending guard failed for segment_id={segment_id}")
        if int(text.get("needs_output_apply") or 0) != 0:
            raise SystemExit(f"needs_output_apply guard failed for segment_id={segment_id}")
        if int(text.get("confirmed_matches_output") or 0) != 1:
            raise SystemExit(f"confirmed_matches_output guard failed for segment_id={segment_id}")
        samples.append(make_sample(record, text, macro_grouped[segment_id]))
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
        route_counts=route_counts,
    )
    summary = summary = {
        "txt": str(txt_path),
        "jsonl": str(jsonl_path),
        "spec": str(spec_path),
        "universe_estimated": universe,
        "total_reviewed": len(samples),
    }
    print(f"txt: {txt_path}")
    print(f"jsonl: {jsonl_path}")
    print(f"spec: {spec_path}")
    print(f"universe_estimated: {universe}")
    print(f"total_reviewed: {len(samples)}")
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
