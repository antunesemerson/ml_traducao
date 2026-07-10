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


SOURCE = "domain_context_religion_holy_site_policy_review_v1"
EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76
EXPECTED_CLOSED_COUNT = 276375
EXPECTED_PENDING_COUNT = 11725
EXPECTED_OUTPUT_APPLY_PENDING_COUNT = 0
EXPECTED_TOTAL = 44
SOURCE_DECISION = "needs_domain_religion_holy_site_policy"

REUSE_KEYS = {
    "holy_site_effect_name_policy",
    "not_requirement_effect_culture_religion_router",
    "not_requirement_effect_culture_policy",
    "effect_list_concept_policy",
    "event_context_after_requirement_effect",
    "residual_repair_after_requirement_effect",
}

ALLOWED_DECISIONS = {
    "domain_religion_holy_site_terminal_policy",
    "domain_religion_holy_site_terminal_policy_with_domain_guard",
    "domain_religion_holy_site_terminal_policy_with_name_guard",
    "domain_religion_holy_site_reuse_holy_site_effect_name_policy",
    "domain_religion_holy_site_reuse_not_requirement_effect_culture_religion_router",
    "domain_religion_holy_site_reuse_not_requirement_effect_culture_policy",
    "domain_religion_holy_site_reuse_effect_list_concept_policy",
    "domain_religion_holy_site_reuse_requirement_effect_event_context_policy",
    "domain_religion_holy_site_reuse_requirement_effect_residual_policy",
    "needs_domain_religion_doctrine_policy",
    "needs_domain_religion_faith_policy",
    "needs_domain_religion_tenet_policy",
    "needs_domain_holy_site_name_location_policy",
    "needs_domain_holy_site_building_modifier_policy",
    "needs_domain_holy_site_title_law_policy",
    "needs_domain_religion_culture_policy",
    "needs_domain_religion_event_context_policy",
    "needs_domain_religion_actor_target_policy",
    "needs_domain_religion_gender_local_player_policy",
    "needs_domain_religion_script_value_policy",
    "needs_domain_religion_scope_getter_policy",
    "needs_domain_religion_residual_repair",
    "needs_domain_religion_dynamic_parser_escape",
    "domain_religion_holy_site_blocked_uncertain",
}

DOMAIN_RE = re.compile(r"religion|faith|doctrine|tenet|holy|god|adherent|pagan|fervor|sacred|ganges", re.I)
RELIGION_RE = re.compile(r"religion|religious|god|adherent|pagan|maitreya|aluk|islam|baltic|tengrism|faith", re.I)
FAITH_RE = re.compile(r"faith|GetFaith|_faith|adherent|adherent_plural|_adj\b|pagan|fervor|god_name", re.I)
DOCTRINE_RE = re.compile(r"doctrine|hostility|special_doctrine", re.I)
TENET_RE = re.compile(r"\btenet\b|tenet_", re.I)
HOLY_SITE_RE = re.compile(r"holy_site|holy site|holy_site_name|holy_site_effect|sacred_rivers|holy_bloodline|holy_war", re.I)
NAME_LOCATION_RE = re.compile(r"_name\b|name_|GetName|Ganges|site|location|place|river|kingdom|maitreya", re.I)
CULTURE_RE = re.compile(r"culture|tradition|heritage|ethos|language|rf_eastern|bai|viet|japan|korea", re.I)
BUILDING_RE = re.compile(r"building|modifier|holding|temple|church|cathedral|shrine|mosque|sanctuary", re.I)
TITLE_LAW_RE = re.compile(r"title|law|government|realm|succession|county|duchy|kingdom|empire|vassal|liege|rank|holding", re.I)
EVENT_RE = re.compile(r"event|\.desc|desc\.|option|toast|dialogue|story|scheme|interaction|memory", re.I)
ACTOR_TARGET_RE = re.compile(r"actor|target|recipient|ROOT\.|FROM\.|SCOPE\.|THIS\.", re.I)
GENDER_RE = re.compile(r"Select_CString|ES_(?:OA|XA|EA|ElLa|DelDela|AlAla|A|O)|Get(?:SheHe|HerHis|HerHim)|local_player|GetPlayer|GetLocalPlayer", re.I)
SCRIPT_VALUE_RE = re.compile(r"ScriptValue|GetScriptValue|script_value|\|V[0-9]?|#P\s*[0-9]|\b[0-9]+\s*%", re.I)
SCOPE_RE = re.compile(r"ROOT\.|FROM\.|SCOPE\.|TARGET\.|CHARACTER\.|\[[^\]]*\.(?:Get|Is|Has)[A-Za-z0-9_]*[^\]]*\]", re.I)
DYNAMIC_RE = re.compile(r"Custom\(|Select_CString|Concept\(|ScriptValue|GetTrait|ROOT\.|FROM\.|SCOPE\.|TARGET\.|\[[^\]]+\]|\$[^$]+\$", re.I)
RESIDUAL_RE = re.compile(r"NÃƒ|ÃƒÆ’|Ã‚|ï¿½|Ã¯Â¿Â½|\b(the|your|you|their|cannot|sera|serÃ¡|mas|mÃ¡s|facil|fÃ¡cil)\b", re.I)


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_domain_context_religion_holy_site_policy_review"
    spec = reports_dir / f"{stamp}_domain_context_religion_holy_site_policy_spec.json"
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
    return {
        "registered_agents": len(registry),
        "observed_agent_keys": len(rows),
        "operational_agents": sum(1 for row in registry if row.get("operational_state") == "operational"),
        "dry_run_agents": sum(1 for row in registry if row.get("operational_state") == "dry_run"),
        "shadow_agents": sum(1 for row in registry if row.get("operational_state") == "shadow"),
        "latest_routing_run_id": int(latest_run["id"]) if latest_run else 0,
    }


def active_reuse_policies(conn: sqlite3.Connection) -> set[str]:
    placeholders = ",".join("?" for _ in REUSE_KEYS)
    rows = conn.execute(
        f"""
        SELECT agent_key
        FROM ml_agent_registry
        WHERE agent_key IN ({placeholders})
          AND status = 'active'
        """,
        tuple(sorted(REUSE_KEYS)),
    ).fetchall()
    return {str(row["agent_key"]) for row in rows}


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
    return {
        "domain_markers": marker(DOMAIN_RE, blob, "ReligionDomain"),
        "religion_markers": marker(RELIGION_RE, blob, "Religion"),
        "faith_markers": marker(FAITH_RE, blob, "FaithName"),
        "doctrine_markers": marker(DOCTRINE_RE, blob, "Doctrine"),
        "tenet_markers": marker(TENET_RE, blob, "Tenet"),
        "holy_site_markers": marker(HOLY_SITE_RE, blob, "HolySurface"),
        "name_location_markers": marker(NAME_LOCATION_RE, blob, "NameLocation"),
        "culture_markers": marker(CULTURE_RE, blob, "CultureReligion"),
        "building_markers": marker(BUILDING_RE, blob, "BuildingModifier"),
        "event_markers": marker(EVENT_RE, blob, "EventContext"),
        "dynamic_markers": marker(DYNAMIC_RE, blob, "DynamicToken"),
        "guard_markers": ["StateClean", "NoOutputApply", "ConfirmedMatchesOutput"],
        "secondary_markers": [
            label
            for pattern, label in [
                (TITLE_LAW_RE, "TitleLaw"),
                (ACTOR_TARGET_RE, "ActorTarget"),
                (GENDER_RE, "GenderLocalPlayer"),
                (SCRIPT_VALUE_RE, "ScriptValue"),
                (SCOPE_RE, "ScopeGetter"),
                (RESIDUAL_RE, "ResidualVisible"),
            ]
            if pattern.search(blob)
        ],
    }


def reuse(policy: str, active: set[str], decision: str, rationale: str) -> tuple[str, str, str, str, str]:
    return (
        decision,
        policy if policy in active else "",
        "" if policy in active else policy,
        policy,
        rationale,
    )


def decide(row: dict[str, Any], active: set[str]) -> tuple[str, str, str, str, str]:
    blob = blob_for(row)
    source_key = str(row.get("source_key") or "")
    if RESIDUAL_RE.search(blob):
        return reuse(
            "residual_repair_after_requirement_effect",
            active,
            "domain_religion_holy_site_reuse_requirement_effect_residual_policy",
            "visible residual marker should reuse requirement/effect residual splitter",
        )
    if re.search(r"holy_site|holy site", blob, re.I):
        return reuse(
            "holy_site_effect_name_policy",
            active,
            "domain_religion_holy_site_reuse_holy_site_effect_name_policy",
            "explicit holy-site marker can reuse registered holy-site effect/name policy",
        )
    if BUILDING_RE.search(blob):
        return ("needs_domain_holy_site_building_modifier_policy", "", "", "domain_holy_site_building_modifier_policy", "building/modifier marker remains inside religion/holy-site domain")
    if EVENT_RE.search(blob):
        return reuse(
            "event_context_after_requirement_effect",
            active,
            "domain_religion_holy_site_reuse_requirement_effect_event_context_policy",
            "event/context marker can reuse requirement/effect event-context splitter",
        )
    if GENDER_RE.search(blob):
        return ("needs_domain_religion_gender_local_player_policy", "", "", "domain_religion_gender_local_player_policy", "gender/local-player marker remains")
    if SCRIPT_VALUE_RE.search(blob):
        return ("needs_domain_religion_script_value_policy", "", "", "domain_religion_script_value_policy", "ScriptValue/numeric marker remains")
    if SCOPE_RE.search(blob):
        return ("needs_domain_religion_scope_getter_policy", "", "", "domain_religion_scope_getter_policy", "scope/getter marker remains")
    if TITLE_LAW_RE.search(blob):
        return ("needs_domain_holy_site_title_law_policy", "", "", "domain_holy_site_title_law_policy", "title/law marker remains inside religion/holy-site domain")
    if DOCTRINE_RE.search(source_key):
        return ("needs_domain_religion_doctrine_policy", "", "", "domain_religion_doctrine_policy", "specific doctrine/hostility key remains")
    if TENET_RE.search(source_key):
        return ("needs_domain_religion_tenet_policy", "", "", "domain_religion_tenet_policy", "specific tenet key remains")
    if RELIGION_RE.search(blob) or FAITH_RE.search(blob):
        return reuse(
            "not_requirement_effect_culture_religion_router",
            active,
            "domain_religion_holy_site_reuse_not_requirement_effect_culture_religion_router",
            "plain religion/faith/name short label should route through registered not_requirement_effect culture/religion router",
        )
    if CULTURE_RE.search(blob):
        return reuse(
            "not_requirement_effect_culture_policy",
            active,
            "domain_religion_holy_site_reuse_not_requirement_effect_culture_policy",
            "culture/religion marker can reuse not_requirement_effect culture policy",
        )
    if NAME_LOCATION_RE.search(blob):
        return ("needs_domain_holy_site_name_location_policy", "", "", "domain_holy_site_name_location_policy", "name/location marker remains without registered route")
    if DYNAMIC_RE.search(blob):
        return ("needs_domain_religion_dynamic_parser_escape", "", "ck3_dynamic_expression_parser_spec", "ck3_dynamic_expression_parser_spec", "dynamic token should escape to parser after religion checks")
    return ("domain_religion_holy_site_blocked_uncertain", "", "", "domain_context_religion_holy_site_policy", "insufficient religion/holy-site subtype evidence")


def convert_sample(row: dict[str, Any], active: set[str]) -> dict[str, Any]:
    groups = marker_groups(row)
    decision, registered, catalog, next_component, rationale = decide(row, active)
    return {
        "record_type": "sample_review",
        "segment_id": int(row["segment_id"]),
        "relative_path": str(row.get("relative_path") or ""),
        "source_key": str(row.get("source_key") or ""),
        "families_open": row.get("families_open") or [],
        "source_decision": SOURCE_DECISION,
        "parent_policy": "domain_context_after_requirement_effect",
        "primary_route": "domain_context_after_requirement_effect",
        "old_text": str(row.get("old_text") or ""),
        "confirmed_text": str(row.get("confirmed_text") or ""),
        "output_text": str(row.get("output_text") or ""),
        **groups,
        "matched_registered_policy": registered,
        "matched_catalog_spec": catalog,
        "religion_holy_site_decision": decision,
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
        "religion_markers",
        "faith_markers",
        "doctrine_markers",
        "tenet_markers",
        "holy_site_markers",
        "name_location_markers",
        "culture_markers",
        "building_markers",
        "event_markers",
        "dynamic_markers",
        "matched_registered_policy",
        "matched_catalog_spec",
        "guard_markers",
        "secondary_markers",
        "religion_holy_site_decision",
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
        if row["religion_holy_site_decision"] not in ALLOWED_DECISIONS:
            raise SystemExit(f"invalid religion_holy_site_decision for {segment_id}: {row['religion_holy_site_decision']}")
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
    decision_counts = Counter(row["religion_holy_site_decision"] for row in samples)
    reuse_count = sum(1 for row in samples if row["religion_holy_site_decision"].startswith("domain_religion_holy_site_reuse_"))
    terminal_count = sum(1 for row in samples if row["religion_holy_site_decision"].startswith("domain_religion_holy_site_terminal_policy"))
    needs_counts = Counter(row["religion_holy_site_decision"] for row in samples if row["religion_holy_site_decision"].startswith("needs_"))
    dominant_decision, dominant_count = decision_counts.most_common(1)[0]
    concentrated_need = next(((key, count) for key, count in needs_counts.most_common() if count >= 15), None)
    if reuse_count >= 18:
        next_prompt = "chat_exec_domain_context_religion_holy_site_policy_catalog_registration_prompt.md"
        assessment = "reuse_splitter_component"
    elif terminal_count >= 18:
        next_prompt = "chat_exec_domain_context_religion_holy_site_terminal_spec_registration_prompt.md"
        assessment = "terminal_component"
    elif concentrated_need:
        slug = concentrated_need[0].replace("needs_domain_", "")
        next_prompt = f"chat_exec_domain_context_{slug}_review_prompt.md"
        assessment = "micro_router_needed"
    else:
        next_prompt = "chat_exec_domain_context_religion_holy_site_blocked_uncertain_review_prompt.md"
        assessment = "fragmented_religion_holy_site_queue"
    marker_fields = [
        "domain_markers",
        "religion_markers",
        "faith_markers",
        "doctrine_markers",
        "tenet_markers",
        "holy_site_markers",
        "name_location_markers",
        "culture_markers",
        "building_markers",
        "event_markers",
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
        "package_assessment": assessment,
        "next_prompt": next_prompt,
    }
    spec = {
        "schema_version": 1,
        "created_for": "read_only_subpolicy_design",
        "parent_policy": "domain_context_after_requirement_effect",
        "policy_id": "domain_context_religion_holy_site_policy",
        "segment_state_run_id": args.segment_state_run_id,
        "ledger_run_id": EXPECTED_LEDGER_RUN_ID,
        "entry_conditions": [
            "domain_context_decision == needs_domain_religion_holy_site_policy",
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
        "religion_holy_site_types": [{"type": key, "sampled": value} for key, value in decision_counts.most_common()],
        "resolution_order": [
            "state guard",
            "residual reuse",
            "explicit holy-site reuse",
            "building/event/gender/script/scope/title-law split",
            "doctrine and tenet split",
            "plain religion/faith/name reuse through not_requirement_effect culture/religion router",
            "culture/name fallback",
            "dynamic parser escape",
        ],
        "next_components": [next_prompt],
        "blocked_conditions": [
            "state guard failed",
            "needs_output_apply != 0",
            "confirmed_matches_output != 1",
            "ambiguous religion/holy-site evidence",
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
        handle.write("Domain context religion/holy-site policy review\n\n")
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
        handle.write("- Deve virar componente read-only real: sim, como componente de reuso/splitter para religion/faith/name.\n")
        handle.write("- Nao gera lifecycle/apply em curto prazo.\n")
        handle.write(f"- Reuso holy_site_effect_name_policy: {sum(1 for row in samples if row['matched_registered_policy'] == 'holy_site_effect_name_policy')}.\n")
        handle.write(f"- Reuso not_requirement_effect: {sum(1 for row in samples if row['matched_registered_policy'].startswith('not_requirement_effect'))}.\n")
        handle.write(f"- Proximo prompt recomendado: {next_prompt}.\n")
    return txt_path, jsonl_path, spec_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Review domain-context religion/holy-site sublane read-only.")
    parser.add_argument("--domain-jsonl", required=True, type=Path)
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    args = parser.parse_args()
    if args.segment_state_run_id != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("segment_state_run_id guard failed")
    rows = read_jsonl(args.domain_jsonl)
    source_samples = [
        row
        for row in rows
        if row.get("record_type") == "sample_review"
        and row.get("domain_context_decision") == SOURCE_DECISION
    ]
    if len(source_samples) != EXPECTED_TOTAL:
        raise SystemExit(f"source religion/holy-site total guard failed: {len(source_samples)} expected {EXPECTED_TOTAL}")
    ids = [int(row["segment_id"]) for row in source_samples]
    if len(ids) != len(set(ids)):
        raise SystemExit("duplicate source segment_id")
    with connect_readonly() as conn:
        state = state_counts(conn, args.segment_state_run_id)
        registry = registry_metrics(conn)
        active = active_reuse_policies(conn)
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
        samples.append(convert_sample(row, active))
    validate_samples(samples)
    txt_path, jsonl_path, spec_path = write_outputs(args=args, state=state, registry=registry, samples=samples)
    decision_counts = Counter(row["religion_holy_site_decision"] for row in samples)
    reuse_count = sum(1 for row in samples if row["religion_holy_site_decision"].startswith("domain_religion_holy_site_reuse_"))
    print(f"txt_report={txt_path}")
    print(f"jsonl_report={jsonl_path}")
    print(f"spec_json={spec_path}")
    print(f"total_reviewed={len(samples)}")
    print("decision_counts=" + json.dumps(dict(decision_counts), ensure_ascii=False, sort_keys=True))
    print(f"reuse_registered_or_cataloged_count={reuse_count}")
    print("terminal_policy_count=0")
    print("ready_lifecycle_future=0")
    print("apply_candidates_future=0")


if __name__ == "__main__":
    main()
