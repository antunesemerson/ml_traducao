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


SOURCE = "not_requirement_effect_culture_policy_review_v1"
EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76
EXPECTED_CLOSED_COUNT = 276375
EXPECTED_PENDING_COUNT = 11725
EXPECTED_OUTPUT_APPLY_PENDING_COUNT = 0
EXPECTED_REGISTERED_AGENTS = 225
EXPECTED_OBSERVED_AGENT_KEYS = 282
EXPECTED_TOTAL = 48
SOURCE_DECISION = "needs_culture_religion_culture_policy"


CULTURE_RE = re.compile(r"culture|cultural|tradi[cç][aã]o|tradition|heritage|ethos|language|innovation|cultura", re.I)
TRADITION_RE = re.compile(r"tradition|tradi[cç][aã]o|custom|costume", re.I)
HERITAGE_RE = re.compile(r"heritage|heran[cç]a|ancestry|ancestral", re.I)
LANGUAGE_ETHOS_RE = re.compile(r"language|idioma|ethos|estoic|bellicose|bureaucratic|communal|courtly|egalitarian|spiritual", re.I)
NAME_LOCATION_RE = re.compile(r"_name\b|name_|location|place|mount|river|city|county|duchy|kingdom|empire|GetName|GetDynasty", re.I)
TITLE_RE = re.compile(r"title|law|government|realm|succession|vassal|liege|rank|holding", re.I)
ARTIFACT_ACTIVITY_RE = re.compile(r"artifact|activity|travel|tournament|legend|hunt|feast|wedding", re.I)
EVENT_RE = re.compile(r"event|\.desc|desc\.|option|toast|dialogue|story|scheme|interaction|memory", re.I)
DYNAMIC_RE = re.compile(r"Custom\(|Select_CString|Concept\(|ScriptValue|GetTrait|ROOT\.|FROM\.|SCOPE\.|TARGET\.|\[[^\]]+\]|\$[^$]+\$", re.I)
RESIDUAL_RE = re.compile(r"NÃ|Ãƒ|Ã‚|ï¿½|\b(the|your|you|their|cannot|sera|será|mas|más|facil|fácil)\b", re.I)


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_not_requirement_effect_culture_policy_review"
    spec = reports_dir / f"{stamp}_not_requirement_effect_culture_policy_spec.json"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), spec


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    probe = conn.execute("PRAGMA query_only").fetchone()
    if int(probe[0]) != 1:
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
        "latest_routing_run_id": int(latest_run["id"]) if latest_run else 0,
    }
    expected = {
        "registered_agents": EXPECTED_REGISTERED_AGENTS,
        "observed_agent_keys": EXPECTED_OBSERVED_AGENT_KEYS,
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


def has(pattern: re.Pattern[str], blob: str, label: str) -> list[str]:
    return [label] if pattern.search(blob) else []


def blob_for(row: dict[str, Any]) -> str:
    return " ".join([
        str(row.get("relative_path") or ""),
        str(row.get("source_key") or ""),
        str(row.get("old_text") or ""),
        str(row.get("output_text") or ""),
        " ".join(row.get("families_open") or []),
    ])


def marker_groups(row: dict[str, Any]) -> dict[str, list[str]]:
    blob = blob_for(row)
    families = set(row.get("families_open") or [])
    return {
        "culture_markers": has(CULTURE_RE, blob, "Culture"),
        "tradition_markers": has(TRADITION_RE, blob, "Tradition"),
        "heritage_markers": has(HERITAGE_RE, blob, "Heritage"),
        "language_ethos_markers": has(LANGUAGE_ETHOS_RE, blob, "LanguageEthos"),
        "name_location_markers": has(NAME_LOCATION_RE, blob, "NameLocation"),
        "title_markers": has(TITLE_RE, blob, "TitleLaw"),
        "semantic_markers": ["Semantic"] if "semantic_review_router" in families else [],
        "guard_markers": ["StateClean", "NoOutputApply", "ConfirmedMatchesOutput"],
        "secondary_markers": [
            label
            for pattern, label in [
                (ARTIFACT_ACTIVITY_RE, "ArtifactActivity"),
                (EVENT_RE, "EventContext"),
                (DYNAMIC_RE, "DynamicToken"),
                (RESIDUAL_RE, "ResidualVisible"),
            ]
            if pattern.search(blob)
        ],
    }


def decide(row: dict[str, Any], groups: dict[str, list[str]]) -> tuple[str, str, str, str, str]:
    blob = blob_for(row)
    families = set(row.get("families_open") or [])
    source_key = str(row.get("source_key") or "")
    relative_path = str(row.get("relative_path") or "")
    if "semantic_review_router" in families:
        return (
            "culture_reuse_semantic_review_router",
            "semantic_review_router",
            "",
            "semantic_review_router",
            "semantic review family is already present and can own this culture item",
        )
    if "short_label_style_microagent" in families:
        return (
            "culture_reuse_short_label_style_policy",
            "",
            "short_label_style_policy",
            "short_label_style_policy",
            "short-label style family is already present",
        )
    if "_name" in source_key or NAME_LOCATION_RE.search(blob):
        return (
            "needs_culture_name_location_policy",
            "",
            "",
            "culture_name_location_policy",
            "culture name/location signal needs its own narrow policy",
        )
    if "ethos" in source_key or LANGUAGE_ETHOS_RE.search(blob):
        return (
            "needs_culture_language_ethos_policy",
            "",
            "",
            "culture_language_ethos_policy",
            "culture ethos/language surface is dominant for this item",
        )
    if "tradition" in source_key or "traditions" in relative_path or TRADITION_RE.search(blob) or HERITAGE_RE.search(blob):
        return (
            "needs_culture_tradition_heritage_policy",
            "",
            "",
            "culture_tradition_heritage_policy",
            "culture tradition/heritage surface needs a dedicated narrow policy",
        )
    if TITLE_RE.search(blob):
        return (
            "needs_culture_title_law_policy",
            "",
            "",
            "culture_title_law_policy",
            "title/law/government marker remains after culture checks",
        )
    if ARTIFACT_ACTIVITY_RE.search(blob):
        return (
            "needs_culture_artifact_activity_policy",
            "",
            "",
            "culture_artifact_activity_policy",
            "artifact/activity marker remains after culture checks",
        )
    if EVENT_RE.search(blob):
        return (
            "needs_culture_event_context",
            "",
            "",
            "culture_event_context",
            "event/context marker remains after culture checks",
        )
    if DYNAMIC_RE.search(blob):
        return (
            "needs_culture_dynamic_parser_escape",
            "",
            "",
            "ck3_dynamic_expression_parser_spec",
            "dynamic token should escape to parser after culture guard",
        )
    if RESIDUAL_RE.search(blob):
        return (
            "needs_culture_residual_repair",
            "",
            "",
            "culture_residual_repair",
            "visible residual marker remains but no apply is proposed",
        )
    if "innovation_" in source_key or "cultural_innovations" in relative_path:
        return (
            "culture_terminal_policy_with_domain_guard",
            "",
            "not_requirement_effect_culture_policy",
            "not_requirement_effect_culture_policy",
            "culture innovation description is terminal/read-only with domain guard",
        )
    return (
        "culture_terminal_policy",
        "",
        "not_requirement_effect_culture_policy",
        "not_requirement_effect_culture_policy",
        "culture description appears terminal for this read-only component",
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
        "primary_gap": "not_requirement_effect",
        "old_text": str(row.get("old_text") or ""),
        "confirmed_text": str(row.get("confirmed_text") or ""),
        "output_text": str(row.get("output_text") or ""),
        **groups,
        "matched_registered_policy": registered,
        "matched_catalog_spec": catalog,
        "culture_decision": decision,
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
    decision_counts = Counter(row["culture_decision"] for row in samples)
    reuse_count = sum(1 for row in samples if row["culture_decision"].startswith("culture_reuse_"))
    terminal_count = sum(1 for row in samples if row["culture_decision"].startswith("culture_terminal_policy"))
    needs_counts = Counter(row["culture_decision"] for row in samples if row["culture_decision"].startswith("needs_"))
    dominant_decision, dominant_count = decision_counts.most_common(1)[0]
    next_prompt = "chat_exec_not_requirement_effect_culture_policy_terminal_spec_registration_prompt.md"
    concentrated_need = next(((key, count) for key, count in needs_counts.most_common() if count >= 15), None)
    if terminal_count >= 24:
        next_prompt = "chat_exec_not_requirement_effect_culture_policy_terminal_spec_registration_prompt.md"
    elif reuse_count >= 24:
        next_prompt = "chat_exec_not_requirement_effect_culture_policy_reuse_spec_registration_prompt.md"
    elif concentrated_need:
        slug = concentrated_need[0].replace("needs_culture_", "")
        next_prompt = f"chat_exec_not_requirement_effect_culture_{slug}_review_prompt.md"
    else:
        next_prompt = "chat_exec_not_requirement_effect_culture_religion_router_registration_prompt.md"
    marker_fields = [
        "culture_markers",
        "tradition_markers",
        "heritage_markers",
        "language_ethos_markers",
        "name_location_markers",
        "title_markers",
        "semantic_markers",
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
        "package_assessment": "terminal_component" if terminal_count >= 24 else ("reuse_component" if reuse_count >= 24 else "micro_router_needed"),
        "next_prompt": next_prompt,
    }
    spec = {
        "schema_version": 1,
        "created_for": "read_only_subpolicy_design",
        "parent_policy": "not_requirement_effect_culture_religion_router",
        "policy_id": "not_requirement_effect_culture_policy",
        "segment_state_run_id": args.segment_state_run_id,
        "ledger_run_id": EXPECTED_LEDGER_RUN_ID,
        "entry_conditions": [
            "source_decision == needs_culture_religion_culture_policy",
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
        "culture_types": [{"type": key, "sampled": value} for key, value in decision_counts.most_common()],
        "resolution_order": [
            "state guard",
            "semantic/short-label reuse",
            "name/location guard",
            "language/ethos guard",
            "tradition/heritage guard",
            "title/event/dynamic/residual fallback",
            "terminal culture/domain guard",
        ],
        "next_components": [next_prompt],
        "blocked_conditions": [
            "state guard failed",
            "needs_output_apply != 0",
            "confirmed_matches_output != 1",
            "ambiguous culture evidence",
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
        handle.write("Not requirement/effect culture policy review\n\n")
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
        handle.write(f"- Sublane coesa o bastante para componente read-only real: {'sim' if terminal_count >= 24 or reuse_count >= 24 else 'ainda nao'}.\n")
        handle.write("- Nao gera lifecycle/apply em curto prazo.\n")
        handle.write(f"- Proximo prompt recomendado: {next_prompt}.\n")
        handle.write(f"- Reuso de semantic/short-label/name/domain: {reuse_count}/{len(samples)}.\n")
    return txt_path, jsonl_path, spec_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Review culture policy sublane under not_requirement_effect.")
    parser.add_argument("--culture-religion-jsonl", required=True, type=Path)
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    args = parser.parse_args()
    if args.segment_state_run_id != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("segment_state_run_id guard failed")
    rows = read_jsonl(args.culture_religion_jsonl)
    source_samples = [
        row for row in rows
        if row.get("record_type") == "sample_review"
        and row.get("culture_religion_decision") == SOURCE_DECISION
    ]
    if len(source_samples) != EXPECTED_TOTAL:
        raise SystemExit(f"source culture total guard failed: {len(source_samples)} expected {EXPECTED_TOTAL}")
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
