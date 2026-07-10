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


SOURCE = "domain_context_landed_title_policy_review_v1"
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
EXPECTED_TOTAL = 61
SOURCE_DECISION = "needs_domain_title_landed_title_policy"

ALLOWED_DECISIONS = {
    "domain_landed_title_terminal_policy",
    "domain_landed_title_terminal_policy_with_domain_guard",
    "domain_landed_title_terminal_policy_with_event_guard",
    "domain_landed_title_reuse_effect_list_concept_policy",
    "domain_landed_title_reuse_building_modifier_effect_policy",
    "domain_landed_title_reuse_requirement_effect_residual_policy",
    "domain_landed_title_reuse_not_requirement_effect_culture_policy",
    "needs_domain_landed_title_de_jure_policy",
    "needs_domain_landed_title_rank_policy",
    "needs_domain_landed_title_location_holding_policy",
    "needs_domain_landed_title_holder_claimant_policy",
    "needs_domain_landed_title_adjective_name_policy",
    "needs_domain_landed_title_government_realm_policy",
    "needs_domain_landed_title_culture_name_policy",
    "needs_domain_landed_title_religion_holy_site_policy",
    "needs_domain_landed_title_event_context_policy",
    "needs_domain_landed_title_actor_target_policy",
    "needs_domain_landed_title_gender_local_player_policy",
    "needs_domain_landed_title_script_value_policy",
    "needs_domain_landed_title_accolade_trait_policy",
    "needs_domain_landed_title_building_modifier_policy",
    "needs_domain_landed_title_scope_getter_policy",
    "needs_domain_landed_title_residual_repair",
    "needs_domain_landed_title_dynamic_parser_escape",
    "domain_landed_title_blocked_uncertain",
}

DOMAIN_RE = re.compile(r"title|landed|county|duchy|kingdom|empire|barony|realm|domain|dynasty|dynn_", re.I)
LANDED_TITLE_RE = re.compile(r"(^|[/\\])(?:[ckdebp]_[a-z0-9_]+)|\b[ckdebp]_[a-z0-9_]+|titles?_l_|county|duchy|kingdom|empire|barony", re.I)
DE_JURE_RE = re.compile(r"de_jure|de jure|de_facto|de facto|rightful|drift|liege_title", re.I)
RANK_RE = re.compile(r"\b(county|counties|duchy|duchies|kingdom|kingdoms|empire|empires|barony|baronies|rank|tier)\b|^[ckdebp]_", re.I)
LOCATION_HOLDING_RE = re.compile(r"capital|location|holding|province|barony|county_modifier|GetTitleByKey|_holding|_location", re.I)
HOLDER_CLAIMANT_RE = re.compile(r"holder|claimant|heir|liege|vassal|ruler|holds|control|possui|detem|detém", re.I)
ADJECTIVE_NAME_RE = re.compile(r"dynn_|dynasty|house|GetName|GetNameNoTier|GetAdjective|adjective|suffix|_name\b|name_|title_name", re.I)
GOVERNMENT_RE = re.compile(r"government|realm|crown|authority|contract|succession|law", re.I)
CULTURE_NAME_RE = re.compile(r"culture|tradition|heritage|ethos|language|bai|viet|japan|korea", re.I)
RELIGION_RE = re.compile(r"religion|faith|doctrine|holy_site|holy site|temple|church", re.I)
EVENT_RE = re.compile(r"event|\.desc|desc\.|option|toast|dialogue|story|scheme|interaction|memory", re.I)
ACTOR_TARGET_RE = re.compile(r"actor|target|recipient|ROOT\.|FROM\.|SCOPE\.|THIS\.", re.I)
GENDER_RE = re.compile(r"Select_CString|ES_(?:OA|XA|EA|ElLa|DelDela|AlAla|A|O)|Get(?:SheHe|HerHis|HerHim)|local_player|GetPlayer|GetLocalPlayer", re.I)
SCRIPT_VALUE_RE = re.compile(r"ScriptValue|GetScriptValue|script_value|\|V[0-9]?|#P\s*[0-9]|\b[0-9]+\s*%", re.I)
ACCOLADE_RE = re.compile(r"accolade|acclaimed_knight|knight|trait|GetTrait|prowess", re.I)
BUILDING_RE = re.compile(r"building|modifier|duchy_building|construct", re.I)
SCOPE_RE = re.compile(r"ROOT\.|FROM\.|SCOPE\.|TARGET\.|CHARACTER\.|\[[^\]]*\.(?:Get|Is|Has)[A-Za-z0-9_]*[^\]]*\]", re.I)
DYNAMIC_RE = re.compile(r"Custom\(|Select_CString|Concept\(|ScriptValue|GetTrait|ROOT\.|FROM\.|SCOPE\.|TARGET\.|\[[^\]]+\]|\$[^$]+\$", re.I)
RESIDUAL_RE = re.compile(r"NÃƒ|ÃƒÆ’|Ã‚|ï¿½|Ã¯Â¿Â½|\b(the|your|you|their|cannot|sera|serÃ¡|mas|mÃ¡s|facil|fÃ¡cil)\b", re.I)


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_domain_context_landed_title_policy_review"
    spec = reports_dir / f"{stamp}_domain_context_landed_title_policy_spec.json"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), spec


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"Invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


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


def state_by_id(conn: sqlite3.Connection, segment_ids: list[int], run_id: int) -> dict[int, dict[str, Any]]:
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT segment_id, state_group, is_closed, needs_output_apply, confirmed_matches_output
        FROM segment_state_items
        WHERE run_id = ?
          AND segment_id IN ({placeholders})
        """,
        (run_id, *segment_ids),
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def blob_for(row: dict[str, Any]) -> str:
    return " ".join(
        [
            str(row.get("relative_path") or ""),
            str(row.get("source_key") or ""),
            str(row.get("old_text") or ""),
            str(row.get("confirmed_text") or ""),
            str(row.get("output_text") or ""),
            " ".join(row.get("families_open") or []),
        ]
    )


def marker(pattern: re.Pattern[str], blob: str, label: str) -> list[str]:
    return [label] if pattern.search(blob) else []


def marker_groups(row: dict[str, Any]) -> dict[str, list[str]]:
    blob = blob_for(row)
    source_key = str(row.get("source_key") or "")
    rank_markers = marker(RANK_RE, blob, "TitleRank")
    if source_key.startswith("c_") and "CountyKey" not in rank_markers:
        rank_markers.append("CountyKey")
    return {
        "domain_markers": marker(DOMAIN_RE, blob, "DomainTitle"),
        "landed_title_markers": marker(LANDED_TITLE_RE, blob, "LandedTitleKey"),
        "de_jure_markers": marker(DE_JURE_RE, blob, "DeJure"),
        "rank_markers": rank_markers,
        "location_holding_markers": marker(LOCATION_HOLDING_RE, blob, "LocationHolding"),
        "holder_claimant_markers": marker(HOLDER_CLAIMANT_RE, blob, "HolderClaimant"),
        "adjective_name_markers": marker(ADJECTIVE_NAME_RE, blob, "DynastySuffixName"),
        "government_realm_markers": marker(GOVERNMENT_RE, blob, "GovernmentRealm"),
        "dynamic_markers": marker(DYNAMIC_RE, blob, "DynamicToken"),
        "guard_markers": ["StateClean", "NoOutputApply", "ConfirmedMatchesOutput"],
        "secondary_markers": [
            label
            for pattern, label in [
                (CULTURE_NAME_RE, "CultureName"),
                (RELIGION_RE, "ReligionHolySite"),
                (EVENT_RE, "EventContext"),
                (ACTOR_TARGET_RE, "ActorTarget"),
                (GENDER_RE, "GenderLocalPlayer"),
                (SCRIPT_VALUE_RE, "ScriptValue"),
                (ACCOLADE_RE, "AccoladeTrait"),
                (BUILDING_RE, "BuildingModifier"),
                (SCOPE_RE, "ScopeGetter"),
                (RESIDUAL_RE, "ResidualVisible"),
            ]
            if pattern.search(blob)
        ],
    }


def decide(row: dict[str, Any], groups: dict[str, list[str]]) -> tuple[str, str, str, str, str]:
    blob = blob_for(row)
    if RESIDUAL_RE.search(blob):
        return (
            "domain_landed_title_reuse_requirement_effect_residual_policy",
            "residual_repair_after_requirement_effect",
            "",
            "residual_repair_after_requirement_effect",
            "visible residual marker should reuse registered residual policy with landed-title guard",
        )
    if BUILDING_RE.search(blob):
        return (
            "domain_landed_title_reuse_building_modifier_effect_policy",
            "building_modifier_effect_policy",
            "",
            "building_modifier_effect_policy",
            "building/modifier marker should reuse building/modifier splitter with landed-title guard",
        )
    if DE_JURE_RE.search(blob):
        return ("needs_domain_landed_title_de_jure_policy", "", "", "domain_landed_title_de_jure_policy", "de jure/de facto/rightful hierarchy marker remains")
    if HOLDER_CLAIMANT_RE.search(blob):
        return ("needs_domain_landed_title_holder_claimant_policy", "", "", "domain_landed_title_holder_claimant_policy", "holder/claimant/heir/liege marker remains")
    if GOVERNMENT_RE.search(blob):
        return ("needs_domain_landed_title_government_realm_policy", "", "", "domain_landed_title_government_realm_policy", "government/realm/law marker remains")
    if RELIGION_RE.search(blob):
        return ("needs_domain_landed_title_religion_holy_site_policy", "", "", "domain_landed_title_religion_holy_site_policy", "religion/holy-site marker remains")
    if EVENT_RE.search(blob):
        return ("needs_domain_landed_title_event_context_policy", "", "", "domain_landed_title_event_context_policy", "event/context marker remains")
    if ACTOR_TARGET_RE.search(blob):
        return ("needs_domain_landed_title_actor_target_policy", "", "", "domain_landed_title_actor_target_policy", "actor/target/recipient marker remains")
    if GENDER_RE.search(blob):
        return ("needs_domain_landed_title_gender_local_player_policy", "", "", "domain_landed_title_gender_local_player_policy", "gender/local-player marker remains")
    if SCRIPT_VALUE_RE.search(blob):
        return ("needs_domain_landed_title_script_value_policy", "", "", "domain_landed_title_script_value_policy", "ScriptValue/numeric marker remains")
    if ACCOLADE_RE.search(blob):
        return ("needs_domain_landed_title_accolade_trait_policy", "", "", "domain_landed_title_accolade_trait_policy", "accolade/trait marker remains")
    if SCOPE_RE.search(blob):
        return ("needs_domain_landed_title_scope_getter_policy", "", "", "domain_landed_title_scope_getter_policy", "scope/getter marker remains")
    if ADJECTIVE_NAME_RE.search(blob):
        return (
            "needs_domain_landed_title_adjective_name_policy",
            "",
            "",
            "domain_landed_title_adjective_name_policy",
            "county title is composed from suffix/dynasty/name tokens; adjective/name subpolicy is dominant",
        )
    if LOCATION_HOLDING_RE.search(blob):
        return ("needs_domain_landed_title_location_holding_policy", "", "", "domain_landed_title_location_holding_policy", "location/holding marker remains")
    if CULTURE_NAME_RE.search(blob):
        return ("needs_domain_landed_title_culture_name_policy", "", "", "domain_landed_title_culture_name_policy", "culture/name marker remains")
    if RANK_RE.search(blob):
        return ("needs_domain_landed_title_rank_policy", "", "", "domain_landed_title_rank_policy", "rank/county/duchy key is the remaining landed-title subtype")
    if DYNAMIC_RE.search(blob):
        return ("needs_domain_landed_title_dynamic_parser_escape", "", "ck3_dynamic_expression_parser_spec", "ck3_dynamic_expression_parser_spec", "dynamic token should escape to parser after landed-title checks")
    if LANDED_TITLE_RE.search(blob):
        return (
            "domain_landed_title_terminal_policy_with_domain_guard",
            "",
            "domain_context_landed_title_policy",
            "domain_context_landed_title_policy",
            "landed title appears terminal/read-only with domain guard",
        )
    return ("domain_landed_title_blocked_uncertain", "", "", "domain_context_landed_title_policy", "insufficient landed-title subtype evidence")


def convert_sample(row: dict[str, Any]) -> dict[str, Any]:
    groups = marker_groups(row)
    decision, registered, catalog, next_component, rationale = decide(row, groups)
    return {
        "record_type": "sample_review",
        "segment_id": int(row["segment_id"]),
        "relative_path": str(row.get("relative_path") or ""),
        "source_key": str(row.get("source_key") or ""),
        "families_open": row.get("families_open") or [],
        "source_decision": SOURCE_DECISION,
        "parent_policy": "domain_context_title_law_policy",
        "primary_route": "domain_context_after_requirement_effect",
        "old_text": str(row.get("old_text") or ""),
        "confirmed_text": str(row.get("confirmed_text") or ""),
        "output_text": str(row.get("output_text") or ""),
        **groups,
        "matched_registered_policy": registered,
        "matched_catalog_spec": catalog,
        "landed_title_decision": decision,
        "next_component": next_component,
        "requires_lifecycle_later": False,
        "requires_apply_later": False,
        "corrected_text": "",
        "rationale": rationale,
    }


def validate_samples(samples: list[dict[str, Any]]) -> None:
    required = {
        "record_type",
        "segment_id",
        "relative_path",
        "source_key",
        "families_open",
        "source_decision",
        "parent_policy",
        "primary_route",
        "old_text",
        "confirmed_text",
        "output_text",
        "domain_markers",
        "landed_title_markers",
        "de_jure_markers",
        "rank_markers",
        "location_holding_markers",
        "holder_claimant_markers",
        "adjective_name_markers",
        "government_realm_markers",
        "dynamic_markers",
        "matched_registered_policy",
        "matched_catalog_spec",
        "guard_markers",
        "secondary_markers",
        "landed_title_decision",
        "next_component",
        "requires_lifecycle_later",
        "requires_apply_later",
        "corrected_text",
        "rationale",
    }
    if len(samples) != EXPECTED_TOTAL:
        raise SystemExit(f"review count mismatch: {len(samples)} expected {EXPECTED_TOTAL}")
    seen: set[int] = set()
    for row in samples:
        missing = required - set(row)
        if missing:
            raise SystemExit(f"missing fields for {row.get('segment_id')}: {sorted(missing)}")
        segment_id = int(row["segment_id"])
        if segment_id in seen:
            raise SystemExit(f"duplicate segment_id: {segment_id}")
        seen.add(segment_id)
        if row["source_decision"] != SOURCE_DECISION:
            raise SystemExit(f"wrong source decision for {segment_id}: {row['source_decision']}")
        if row["landed_title_decision"] not in ALLOWED_DECISIONS:
            raise SystemExit(f"invalid landed_title_decision for {segment_id}: {row['landed_title_decision']}")
        if row["requires_apply_later"]:
            raise SystemExit(f"requires_apply_later unexpectedly true for {segment_id}")
        if row["requires_lifecycle_later"]:
            raise SystemExit(f"requires_lifecycle_later unexpectedly true for {segment_id}")


def write_outputs(
    *,
    args: argparse.Namespace,
    state: dict[str, int],
    registry: dict[str, int],
    samples: list[dict[str, Any]],
) -> tuple[Path, Path, Path]:
    txt_path, jsonl_path, spec_path = output_paths()
    decision_counts = Counter(row["landed_title_decision"] for row in samples)
    reuse_count = sum(1 for row in samples if row["landed_title_decision"].startswith("domain_landed_title_reuse_"))
    terminal_count = sum(1 for row in samples if row["landed_title_decision"].startswith("domain_landed_title_terminal_policy"))
    needs_counts = Counter(row["landed_title_decision"] for row in samples if row["landed_title_decision"].startswith("needs_"))
    dominant_decision, dominant_count = decision_counts.most_common(1)[0]
    concentrated_need = next(((key, count) for key, count in needs_counts.most_common() if count >= 18), None)
    if concentrated_need:
        slug = concentrated_need[0].replace("needs_domain_landed_title_", "")
        next_prompt = f"chat_exec_domain_context_landed_title_{slug}_review_prompt.md"
    elif reuse_count >= 25:
        next_prompt = "chat_exec_domain_context_landed_title_policy_catalog_registration_prompt.md"
    elif terminal_count >= 25:
        next_prompt = "chat_exec_domain_context_landed_title_terminal_spec_registration_prompt.md"
    else:
        next_prompt = "chat_exec_domain_context_after_requirement_effect_religion_holy_site_policy_review_prompt.md"
    marker_fields = [
        "domain_markers",
        "landed_title_markers",
        "de_jure_markers",
        "rank_markers",
        "location_holding_markers",
        "holder_claimant_markers",
        "adjective_name_markers",
        "government_realm_markers",
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
        "ledger_run_id": EXPECTED_LEDGER_RUN_ID,
        **state,
        **registry,
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
        "package_assessment": "micro_router_needed" if concentrated_need else ("reuse_component" if reuse_count >= 25 else ("terminal_component" if terminal_count >= 25 else "fragmented_landed_title_queue")),
        "next_prompt": next_prompt,
    }
    spec = {
        "schema_version": 1,
        "created_for": "read_only_subpolicy_design",
        "parent_policy": "domain_context_title_law_policy",
        "policy_id": "domain_context_landed_title_policy",
        "segment_state_run_id": args.segment_state_run_id,
        "ledger_run_id": EXPECTED_LEDGER_RUN_ID,
        "entry_conditions": [
            "title_law_decision == needs_domain_title_landed_title_policy",
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
        "landed_title_types": [{"type": key, "sampled": value} for key, value in decision_counts.most_common()],
        "resolution_order": [
            "state guard",
            "registered/cataloged reuse",
            "de jure and holder/claimant split",
            "government/realm, religion, event, actor/target split",
            "gender/local-player, ScriptValue, accolade/trait, scope split",
            "adjective/name and dynasty suffix split",
            "location/holding and rank split",
            "dynamic parser escape",
            "terminal landed-title domain guard",
        ],
        "next_components": [next_prompt],
        "blocked_conditions": [
            "state guard failed",
            "needs_output_apply != 0",
            "confirmed_matches_output != 1",
            "ambiguous landed-title evidence",
        ],
        "promotion_gate": "read_only_component_only_no_apply_no_lifecycle",
        "observed_decision_counts": dict(decision_counts),
    }
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(summary, ensure_ascii=False, sort_keys=True) + "\n")
        for field, counts in marker_counts.items():
            for marker_name, count in counts.items():
                handle.write(json.dumps({"record_type": f"top_{field}", "value": marker_name, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for family, count in family_counts.most_common(20):
            handle.write(json.dumps({"record_type": "top_family", "family": family, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for sample in samples:
            handle.write(json.dumps(sample, ensure_ascii=False, sort_keys=True) + "\n")
    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("Domain context landed title policy review\n\n")
        for key in [
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
        handle.write("\nTop markers\n")
        for field, counts in marker_counts.items():
            handle.write(f"- {field}: {counts}\n")
        handle.write("\nRespostas objetivas\n")
        handle.write("- Deve virar componente read-only real: ainda nao; ha sublane estreita dominante de adjective/name.\n")
        handle.write("- Nao gera lifecycle/apply em curto prazo.\n")
        handle.write("- Registrar agora: nao; aguardar o review estreito de adjective/name e, em seguida, religion/holy-site ou government/realm.\n")
        handle.write(f"- Proximo prompt recomendado: {next_prompt}.\n")
    return txt_path, jsonl_path, spec_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Review domain-context landed title sublane read-only.")
    parser.add_argument("--title-law-jsonl", required=True, type=Path)
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    args = parser.parse_args()
    if args.segment_state_run_id != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("segment_state_run_id guard failed")
    rows = read_jsonl(args.title_law_jsonl)
    source_samples = [
        row
        for row in rows
        if row.get("record_type") == "sample_review"
        and row.get("title_law_decision") == SOURCE_DECISION
    ]
    if len(source_samples) != EXPECTED_TOTAL:
        raise SystemExit(f"source landed title total guard failed: {len(source_samples)} expected {EXPECTED_TOTAL}")
    ids = [int(row["segment_id"]) for row in source_samples]
    if len(ids) != len(set(ids)):
        raise SystemExit("duplicate source segment_id")
    with connect_readonly() as conn:
        state = state_counts(conn, args.segment_state_run_id)
        registry = registry_metrics(conn)
        state_rows = state_by_id(conn, ids, args.segment_state_run_id)
    samples: list[dict[str, Any]] = []
    for row in source_samples:
        segment_id = int(row["segment_id"])
        state_row = state_rows.get(segment_id)
        if not state_row:
            raise SystemExit(f"missing state row for segment_id={segment_id}")
        if str(state_row.get("state_group") or "") != "pending" or int(state_row.get("is_closed") or 0) != 0:
            raise SystemExit(f"pending guard failed for segment_id={segment_id}")
        if int(state_row.get("needs_output_apply") or 0) != 0:
            raise SystemExit(f"needs_output_apply guard failed for segment_id={segment_id}")
        if int(state_row.get("confirmed_matches_output") or 0) != 1:
            raise SystemExit(f"confirmed_matches_output guard failed for segment_id={segment_id}")
        samples.append(convert_sample(row))
    validate_samples(samples)
    txt_path, jsonl_path, spec_path = write_outputs(args=args, state=state, registry=registry, samples=samples)
    decision_counts = Counter(row["landed_title_decision"] for row in samples)
    print(f"txt_report={txt_path}")
    print(f"jsonl_report={jsonl_path}")
    print(f"spec_json={spec_path}")
    print(f"total_reviewed={len(samples)}")
    print("decision_counts=" + json.dumps(dict(decision_counts), ensure_ascii=False, sort_keys=True))
    print("reuse_registered_or_cataloged_count=0")
    print("terminal_policy_count=0")
    print("ready_lifecycle_future=0")
    print("apply_candidates_future=0")


if __name__ == "__main__":
    main()
