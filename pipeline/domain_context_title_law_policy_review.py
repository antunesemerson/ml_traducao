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


SOURCE = "domain_context_title_law_policy_review_v1"
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
EXPECTED_TOTAL = 69
SOURCE_DECISION = "needs_domain_title_law_policy"


DOMAIN_RE = re.compile(r"title|law|government|realm|vassal|liege|rank|county|duchy|kingdom|empire|barony|holding|dynasty|house|culture|religion|building", re.I)
TITLE_LAW_RE = re.compile(r"title|law|government|realm|succession|county|duchy|kingdom|empire|barony|vassal|liege|rank|holding|de_jure|crown", re.I)
GOVERNMENT_RE = re.compile(r"government|realm|vassal|liege|contract|authority|crown", re.I)
LANDED_TITLE_RE = re.compile(r"\b[ckdebp]_[a-z0-9_]+|county|duchy|kingdom|empire|barony|de_jure|landed|titles?_l_", re.I)
CULTURE_NAME_RE = re.compile(r"culture|cultural|dynasty|house|dynn_|_name\b|name_|adjective|suffix", re.I)
RELIGION_RE = re.compile(r"religion|faith|doctrine|holy_site|holy site|temple|church", re.I)
EVENT_RE = re.compile(r"event|\.desc|desc\.|option|toast|dialogue|story|scheme|interaction|memory", re.I)
DYNAMIC_RE = re.compile(r"Custom\(|Select_CString|Concept\(|ScriptValue|GetTrait|ROOT\.|FROM\.|SCOPE\.|TARGET\.|\[[^\]]+\]|\$[^$]+\$", re.I)
GENDER_RE = re.compile(r"Select_CString|ES_(?:OA|XA|EA|ElLa|DelDela|AlAla|A|O)|Get(?:SheHe|HerHis|HerHim)|local_player|GetPlayer|GetLocalPlayer", re.I)
SCRIPT_VALUE_RE = re.compile(r"ScriptValue|GetScriptValue|\|V[0-9]?|\|=\+?0|[0-9]+%", re.I)
ACCOLADE_RE = re.compile(r"accolade|acclaimed_knight|knight|trait|GetTrait|prowess", re.I)
BUILDING_RE = re.compile(r"building|modifier|holding|construct|duchy_building", re.I)
SCOPE_RE = re.compile(r"ROOT\.|FROM\.|SCOPE\.|TARGET\.|CHARACTER\.|Get[A-Za-z0-9_]+\(", re.I)
RESIDUAL_RE = re.compile(r"NÃ|Ãƒ|Ã‚|ï¿½|\b(the|your|you|their|cannot|sera|será|mas|más|facil|fácil)\b", re.I)


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_domain_context_title_law_policy_review"
    spec = reports_dir / f"{stamp}_domain_context_title_law_policy_spec.json"
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
    return " ".join([
        str(row.get("relative_path") or ""),
        str(row.get("source_key") or ""),
        str(row.get("old_text") or ""),
        str(row.get("output_text") or ""),
        " ".join(row.get("families_open") or []),
    ])


def has(pattern: re.Pattern[str], blob: str, label: str) -> list[str]:
    return [label] if pattern.search(blob) else []


def marker_groups(row: dict[str, Any]) -> dict[str, list[str]]:
    blob = blob_for(row)
    return {
        "domain_markers": has(DOMAIN_RE, blob, "Domain"),
        "title_law_markers": has(TITLE_LAW_RE, blob, "TitleLaw"),
        "government_realm_markers": has(GOVERNMENT_RE, blob, "GovernmentRealm"),
        "landed_title_markers": has(LANDED_TITLE_RE, blob, "LandedTitle"),
        "culture_name_markers": has(CULTURE_NAME_RE, blob, "CultureName"),
        "religion_markers": has(RELIGION_RE, blob, "Religion"),
        "event_markers": has(EVENT_RE, blob, "Event"),
        "dynamic_markers": has(DYNAMIC_RE, blob, "DynamicToken"),
        "guard_markers": ["StateClean", "NoOutputApply", "ConfirmedMatchesOutput"],
        "secondary_markers": [
            label
            for pattern, label in [
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
    source_key = str(row.get("source_key") or "")
    relative_path = str(row.get("relative_path") or "")
    families = set(row.get("families_open") or [])
    if RESIDUAL_RE.search(blob) or "spanish_residual_microagent" in families:
        return (
            "domain_title_law_reuse_requirement_effect_residual_policy",
            "residual_repair_after_requirement_effect",
            "",
            "residual_repair_after_requirement_effect",
            "visible residual marker should reuse registered residual policy",
        )
    if BUILDING_RE.search(blob):
        return (
            "domain_title_law_reuse_building_modifier_effect_policy",
            "building_modifier_effect_policy",
            "",
            "building_modifier_effect_policy",
            "building/modifier marker should reuse building/modifier splitter",
        )
    if "title_policy_microagent" in families and (LANDED_TITLE_RE.search(blob) or re.match(r"^[ckdebp]_", source_key)):
        return (
            "needs_domain_title_landed_title_policy",
            "",
            "",
            "domain_title_landed_title_policy",
            "landed title key/path is the dominant title/law subtype",
        )
    if re.search(r"adjective|_adj\b|suffix|dynn_", blob, re.I):
        return (
            "needs_domain_title_adjective_name_policy",
            "",
            "",
            "domain_title_adjective_name_policy",
            "title adjective/name/dynasty suffix signal needs a narrow policy",
        )
    if GOVERNMENT_RE.search(blob):
        return (
            "needs_domain_title_government_realm_policy",
            "",
            "",
            "domain_title_government_realm_policy",
            "government/realm/vassalage marker remains",
        )
    if re.search(r"succession|law|crown", blob, re.I):
        return (
            "needs_domain_title_law_succession_policy",
            "",
            "",
            "domain_title_law_succession_policy",
            "law/succession/crown-authority marker remains",
        )
    if RELIGION_RE.search(blob):
        return (
            "needs_domain_title_religion_holy_site_policy",
            "",
            "",
            "domain_title_religion_holy_site_policy",
            "religion/holy-site marker remains inside title/law domain",
        )
    if EVENT_RE.search(blob):
        return (
            "needs_domain_title_event_context_policy",
            "",
            "",
            "domain_title_event_context_policy",
            "event/context marker remains inside title/law domain",
        )
    if GENDER_RE.search(blob):
        return (
            "needs_domain_title_gender_local_player_policy",
            "",
            "",
            "domain_title_gender_local_player_policy",
            "gender/local-player marker remains inside title/law domain",
        )
    if SCRIPT_VALUE_RE.search(blob):
        return (
            "needs_domain_title_script_value_policy",
            "",
            "",
            "domain_title_script_value_policy",
            "ScriptValue marker remains inside title/law domain",
        )
    if ACCOLADE_RE.search(blob):
        return (
            "needs_domain_title_accolade_trait_policy",
            "",
            "",
            "domain_title_accolade_trait_policy",
            "accolade/trait marker remains inside title/law domain",
        )
    if SCOPE_RE.search(blob):
        return (
            "needs_domain_title_scope_getter_policy",
            "",
            "",
            "domain_title_scope_getter_policy",
            "scope/getter marker remains inside title/law domain",
        )
    if DYNAMIC_RE.search(blob):
        return (
            "needs_domain_title_dynamic_parser_escape",
            "",
            "",
            "ck3_dynamic_expression_parser_spec",
            "dynamic token should escape to parser after title/law checks",
        )
    if CULTURE_NAME_RE.search(blob):
        return (
            "needs_domain_title_culture_name_policy",
            "",
            "",
            "domain_title_culture_name_policy",
            "culture/name marker remains after landed-title checks",
        )
    if TITLE_LAW_RE.search(blob):
        return (
            "needs_domain_title_rank_title_policy",
            "",
            "",
            "domain_title_rank_title_policy",
            "generic rank/title marker remains",
        )
    return (
        "domain_title_law_terminal_policy_with_domain_guard",
        "",
        "domain_context_title_law_policy",
        "domain_context_title_law_policy",
        "title/law domain appears terminal/read-only",
    )


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
        "primary_route": "domain_context_after_requirement_effect",
        "old_text": str(row.get("old_text") or ""),
        "confirmed_text": str(row.get("confirmed_text") or ""),
        "output_text": str(row.get("output_text") or ""),
        **groups,
        "matched_registered_policy": registered,
        "matched_catalog_spec": catalog,
        "title_law_decision": decision,
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
    samples: list[dict[str, Any]],
) -> tuple[Path, Path, Path]:
    txt_path, jsonl_path, spec_path = output_paths()
    decision_counts = Counter(row["title_law_decision"] for row in samples)
    reuse_count = sum(1 for row in samples if row["title_law_decision"].startswith("domain_title_law_reuse_"))
    terminal_count = sum(1 for row in samples if row["title_law_decision"].startswith("domain_title_law_terminal_policy"))
    needs_counts = Counter(row["title_law_decision"] for row in samples if row["title_law_decision"].startswith("needs_"))
    dominant_decision, dominant_count = decision_counts.most_common(1)[0]
    next_prompt = "chat_exec_domain_context_after_requirement_effect_religion_holy_site_policy_review_prompt.md"
    concentrated_need = next(((key, count) for key, count in needs_counts.most_common() if count >= 20), None)
    if concentrated_need:
        slug = concentrated_need[0].replace("needs_domain_title_", "")
        next_prompt = f"chat_exec_domain_context_title_law_{slug}_review_prompt.md"
    elif reuse_count >= 25:
        next_prompt = "chat_exec_domain_context_title_law_policy_registration_prompt.md"
    elif terminal_count >= 25:
        next_prompt = "chat_exec_domain_context_title_law_terminal_registration_prompt.md"
    marker_fields = [
        "domain_markers",
        "title_law_markers",
        "government_realm_markers",
        "landed_title_markers",
        "culture_name_markers",
        "religion_markers",
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
        "package_assessment": "micro_router_needed" if concentrated_need else ("reuse_component" if reuse_count >= 25 else ("terminal_component" if terminal_count >= 25 else "fragmented_title_law_queue")),
        "next_prompt": next_prompt,
    }
    spec = {
        "schema_version": 1,
        "created_for": "read_only_subpolicy_design",
        "parent_policy": "domain_context_after_requirement_effect",
        "policy_id": "domain_context_title_law_policy",
        "segment_state_run_id": args.segment_state_run_id,
        "ledger_run_id": EXPECTED_LEDGER_RUN_ID,
        "entry_conditions": [
            "source_decision == needs_domain_title_law_policy",
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
        "title_law_types": [{"type": key, "sampled": value} for key, value in decision_counts.most_common()],
        "resolution_order": [
            "state guard",
            "residual/building reuse",
            "landed title guard",
            "adjective/name guard",
            "government/realm and law/succession split",
            "religion/event/gender/script/accolade/building/scope split",
            "dynamic parser escape",
            "terminal title/law domain guard",
        ],
        "next_components": [next_prompt],
        "blocked_conditions": [
            "state guard failed",
            "needs_output_apply != 0",
            "confirmed_matches_output != 1",
            "ambiguous title/law evidence",
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
        handle.write("Domain context title/law policy review\n\n")
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
        handle.write("\nRespostas objetivas\n")
        handle.write(f"- Deve virar componente read-only real: {'sim' if reuse_count >= 25 or terminal_count >= 25 else 'ainda nao; sublane estreita dominante'}.\n")
        handle.write("- Nao gera lifecycle/apply em curto prazo.\n")
        handle.write(f"- Proximo prompt recomendado: {next_prompt}.\n")
    return txt_path, jsonl_path, spec_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Review domain-context title/law sublane read-only.")
    parser.add_argument("--domain-jsonl", required=True, type=Path)
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    args = parser.parse_args()
    if args.segment_state_run_id != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("segment_state_run_id guard failed")
    rows = read_jsonl(args.domain_jsonl)
    source_samples = [
        row for row in rows
        if row.get("record_type") == "sample_review"
        and row.get("domain_context_decision") == SOURCE_DECISION
    ]
    if len(source_samples) != EXPECTED_TOTAL:
        raise SystemExit(f"source title/law total guard failed: {len(source_samples)} expected {EXPECTED_TOTAL}")
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
    if any(row["source_decision"] != SOURCE_DECISION for row in samples):
        raise SystemExit("source_decision guard failed")
    if any(row["requires_apply_later"] for row in samples):
        raise SystemExit("requires_apply_later guard failed")
    if any(row["requires_lifecycle_later"] for row in samples):
        raise SystemExit("requires_lifecycle_later guard failed")
    txt_path, jsonl_path, spec_path = write_outputs(args=args, state=state, registry=registry, samples=samples)
    print(f"txt: {txt_path}")
    print(f"jsonl: {jsonl_path}")
    print(f"spec: {spec_path}")
    print(f"total_reviewed: {len(samples)}")


if __name__ == "__main__":
    main()
