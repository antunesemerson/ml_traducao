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
import requirement_effect_router_readonly as req_router


ALLOWED_DECISIONS = {
    "tooltip_ready_false_reopen",
    "tooltip_ready_lifecycle",
    "needs_scope_getter_requirement_policy",
    "needs_concept_requirement_policy",
    "needs_gender_local_player_requirement_policy",
    "needs_name_nickname_requirement_guard",
    "needs_title_law_requirement_policy",
    "needs_trait_accolade_requirement_policy",
    "needs_artifact_activity_requirement_policy",
    "needs_building_modifier_requirement_policy",
    "needs_script_value_requirement_policy",
    "needs_domain_context_after_tooltip",
    "needs_event_context_after_tooltip",
    "needs_dynamic_parser_after_tooltip",
    "needs_residual_repair_after_tooltip",
    "tooltip_blocked_uncertain",
}

TOOLTIP_MARKERS: list[tuple[str, re.Pattern[str]]] = [
    ("Tooltip", re.compile(r"tooltip|_tt\b|#T\b", re.I)),
    ("Requirement", re.compile(r"requirement|required|trigger|valid|allowed|cannot|can_|unlock|available|need|must", re.I)),
    ("Condition", re.compile(r"NO_CHANCE|invalid|valid|blocked|disabled|missing|has_|is_|not_", re.I)),
]

SECONDARY_MARKERS: list[tuple[str, re.Pattern[str]]] = [
    ("ScopeGetter", re.compile(r"ROOT\.|FROM\.|SCOPE\.|TARGET\.|CHARACTER\.|Get[A-Za-z0-9_]+\(", re.I)),
    ("Concept", re.compile(r"\[[A-Za-z0-9_]+\|[^\]]+\]|\[Concept\(|Concept\(", re.I)),
    ("GenderLocalPlayer", re.compile(r"Select_CString|ES_(?:OA|XA|EA|ElLa|DelDela|AlAla|A|O)|Get(?:SheHe|HerHis|HerHim|WomanMan|WomenMen)|local_player|GetPlayer|GetLocalPlayer|\bvoc(?:ê|Ãª)\b|\bseu\b|\bsua\b", re.I)),
    ("NameNickname", re.compile(r"name|nickname|dynasty|house|GetName|GetFirstName|GetDynasty|GetHouse|epithet", re.I)),
    ("TitleLaw", re.compile(r"title|law|government|realm|succession|county|duchy|kingdom|empire|vassal|liege|rank|holding", re.I)),
    ("TraitAccolade", re.compile(r"trait|GetTrait|accolade|acclaimed_knight|knight|prowess|skills", re.I)),
    ("ArtifactActivity", re.compile(r"artifact|activity|travel|tournament|legend|item|journey|hunt|feast|wedding", re.I)),
    ("BuildingModifier", re.compile(r"building|buildings?|modifier|GetModifier|construct|duchy_building|economic|tax|development", re.I)),
    ("ScriptValue", re.compile(r"ScriptValue|GetScriptValue|\|V[0-9]?|\|=\+?0|[0-9]+%", re.I)),
    ("Domain", re.compile(r"culture|religion|faith|doctrine|tradition|dynasty|house", re.I)),
    ("Event", re.compile(r"event|\.desc|desc\.|option|toast|dialogue|story|scheme|interaction|memory", re.I)),
    ("DynamicToken", re.compile(r"Custom\(|Select_CString|Concept\(|ScriptValue|GetTrait|ROOT\.|FROM\.|SCOPE\.|TARGET\.|\[[^\]]+\]|\$[^$]+\$", re.I)),
    ("ResidualVisible", re.compile(r"NÃƒ|ÃƒÆ’|Ã‚|ï¿½|\b(?:the|your|you|their|cannot|consiguio|consiguiÃ³|sentisteis|sintieron|sera|serÃ¡|mas|mÃ¡s|facil|fÃ¡cil)\b", re.I)),
]


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
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


def marker_names(patterns: list[tuple[str, re.Pattern[str]]], blob: str) -> list[str]:
    return [name for name, pattern in patterns if pattern.search(blob)]


def fetch_sample_texts(conn: sqlite3.Connection, segment_ids: list[int]) -> dict[int, dict[str, str]]:
    if not segment_ids:
        return {}
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT
            s.segment_id,
            src.old_text,
            out.portuguese_text AS output_text
        FROM segment_state_items s
        LEFT JOIN source_segments src
          ON src.id = s.segment_id
        LEFT JOIN output_segments out
          ON out.segment_id = s.segment_id
        WHERE s.run_id = ?
          AND s.segment_id IN ({placeholders})
        """,
        (400, *segment_ids),
    ).fetchall()
    return {
        int(row["segment_id"]): {
            "old_text": str(row["old_text"] or ""),
            "confirmed_text": "",
            "output_text": str(row["output_text"] or ""),
        }
        for row in rows
    }


def pending_state_ok(rows: list[dict[str, Any]]) -> bool:
    first = rows[0]
    return bool(first.get("state_group") == "pending" and int(first.get("is_closed") or 0) == 0)


def classify(markers: list[str], state_rows: list[dict[str, Any]], text: dict[str, str]) -> tuple[str, str, bool, str]:
    marker_set = set(markers)
    first = state_rows[0]
    if not pending_state_ok(state_rows):
        return "tooltip_blocked_uncertain", "human_review_or_evidence_collection", False, "segment is no longer pending in selected state run"
    if "ResidualVisible" in marker_set:
        return "needs_residual_repair_after_tooltip", "residual_dependency_filtered_repair", False, "visible residual/mojibake blocks tooltip promotion"
    if "GenderLocalPlayer" in marker_set:
        return "needs_gender_local_player_requirement_policy", "gender_local_player_requirement_policy", False, "tooltip depends on local-player/gender perspective"
    if "ScriptValue" in marker_set:
        return "needs_script_value_requirement_policy", "script_value_requirement_policy", False, "script value or numeric expression is present"
    if "ScopeGetter" in marker_set:
        return "needs_scope_getter_requirement_policy", "scope_getter_requirement_policy", False, "scope/getter expression must be resolved before tooltip can be considered safe"
    if "Concept" in marker_set:
        return "needs_concept_requirement_policy", "concept_requirement_policy", False, "concept expression must be resolved before tooltip can be considered safe"
    if "NameNickname" in marker_set:
        return "needs_name_nickname_requirement_guard", "name_nickname_requirement_guard", False, "name/dynasty/nickname guard remains after generic scope/getter routing"
    if "TitleLaw" in marker_set:
        return "needs_title_law_requirement_policy", "title_law_requirement_policy", False, "title/law/government requirement vocabulary is present"
    if "TraitAccolade" in marker_set:
        return "needs_trait_accolade_requirement_policy", "trait_accolade_requirement_policy", False, "trait/accolade/knight requirement vocabulary is present"
    if "ArtifactActivity" in marker_set:
        return "needs_artifact_activity_requirement_policy", "artifact_activity_requirement_policy", False, "artifact/activity/travel requirement vocabulary is present"
    if "BuildingModifier" in marker_set:
        return "needs_building_modifier_requirement_policy", "building_modifier_requirement_policy", False, "building/modifier requirement vocabulary is present"
    if "Domain" in marker_set:
        return "needs_domain_context_after_tooltip", "domain_context_composer", False, "domain context remains after tooltip routing"
    if "Event" in marker_set:
        return "needs_event_context_after_tooltip", "event_context_composer", False, "event context remains after tooltip routing"
    if "DynamicToken" in marker_set:
        return "needs_dynamic_parser_after_tooltip", "ck3_dynamic_symbolic_parser", False, "dynamic token remains after tooltip routing"
    if text["output_text"] and int(first.get("confirmed_matches_output") or 0) == 1 and int(first.get("needs_output_apply") or 0) == 0:
        if int(first.get("needs_reopen") or 0) == 1:
            return "tooltip_ready_false_reopen", "false_reopen_lifecycle_bridge", True, "plain tooltip candidate for future false-reopen lifecycle"
        return "tooltip_ready_lifecycle", "requirement_tooltip_lifecycle_bridge", True, "plain tooltip candidate for future lifecycle"
    return "tooltip_blocked_uncertain", "human_review_or_evidence_collection", False, "insufficient text evidence for a narrower tooltip subtype"


def route_records_from_db(
    conn: sqlite3.Connection,
    segment_state_run_id: int,
    ledger_run_id: int,
) -> tuple[list[dict[str, Any]], dict[int, list[dict[str, Any]]]]:
    req_router.fetch_runs(conn, segment_state_run_id, ledger_run_id)
    grouped = req_router.fetch_pending_rows(conn, segment_state_run_id, ledger_run_id)
    records: list[dict[str, Any]] = []
    for segment_id, rows in grouped.items():
        blob = req_router.blob_for(rows)
        markers = req_router.detect_markers(blob)
        route, reason = req_router.route_for(blob, markers)
        if route != "requirement_tooltip_policy":
            continue
        families = req_router.families_for(rows)
        key = req_router.cohort_key(route, families, markers)
        records.append(
            {
                "segment_id": segment_id,
                "relative_path": str(rows[0].get("relative_path") or ""),
                "source_key": str(rows[0].get("source_key") or ""),
                "families_open": list(families),
                "cohort_key": key,
                "router_markers": markers,
                "router_reason": reason,
                "blob": blob,
            }
        )
    records.sort(key=lambda row: (row["relative_path"], row["source_key"], row["segment_id"]))
    return records, grouped


def sample_records(records: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    by_cohort: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        by_cohort[record["cohort_key"]].append(record)
    top = [cohort for cohort, _ in Counter(record["cohort_key"] for record in records).most_common()]
    selected: list[dict[str, Any]] = []
    seen: set[int] = set()

    quotas: list[tuple[str, int]] = []
    if len(top) > 0:
        quotas.append((top[0], 45))
    if len(top) > 1:
        quotas.append((top[1], 45))
    if len(top) > 2:
        quotas.append((top[2], 35))
    culture = next((cohort for cohort in top if "culture_semantic_microagent" in cohort or "::Domain" in cohort), None)
    if culture and culture not in {cohort for cohort, _ in quotas}:
        quotas.append((culture, 30))

    for cohort, quota in quotas:
        for record in by_cohort[cohort][:quota]:
            if len(selected) >= limit:
                break
            if record["segment_id"] in seen:
                continue
            selected.append(record)
            seen.add(record["segment_id"])

    minor_dynamic = [
        record
        for record in records
        if record["segment_id"] not in seen
        and "dynamic_ck3_expression_microagent" in record["cohort_key"]
        and "short_label_style_microagent" in record["cohort_key"]
    ]
    for record in minor_dynamic[:50]:
        if len(selected) >= limit:
            break
        selected.append(record)
        seen.add(record["segment_id"])

    for record in records:
        if len(selected) >= limit:
            break
        if record["segment_id"] in seen:
            continue
        selected.append(record)
        seen.add(record["segment_id"])
    return selected[:limit]


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_requirement_tooltip_policy_review"
    spec = reports_dir / f"{stamp}_requirement_tooltip_policy_spec.json"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), spec


def build_spec(run_id: int, ledger_run_id: int, decisions: Counter[str], markers: Counter[str]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "created_for": "read_only_subpolicy_design",
        "parent_policy": "requirement_effect_list_policy",
        "policy_id": "requirement_tooltip_policy",
        "segment_state_run_id": run_id,
        "ledger_run_id": ledger_run_id,
        "entry_conditions": [
            "parent router route == requirement_tooltip_policy",
            "segment remains pending in segment_state_run_id 400",
            "tooltip/requirement marker is present in source_key or evidence_text",
        ],
        "subtypes": [{"decision": decision, "sampled": count} for decision, count in decisions.most_common()],
        "resolution_order": [
            "state guard",
            "residual visible guard",
            "gender/local-player perspective",
            "name/nickname/title/trait/artifact/building domain guards",
            "script value",
            "scope/getter",
            "concept",
            "domain/event context",
            "dynamic parser after tooltip",
        ],
        "next_components": [
            "gender_local_player_requirement_policy",
            "name_nickname_requirement_guard",
            "title_law_requirement_policy",
            "trait_accolade_requirement_policy",
            "artifact_activity_requirement_policy",
            "building_modifier_requirement_policy",
            "script_value_requirement_policy",
            "scope_getter_requirement_policy",
            "concept_requirement_policy",
            "domain_context_composer",
            "event_context_composer",
            "ck3_dynamic_symbolic_parser",
            "residual_dependency_filtered_repair",
        ],
        "blocked_conditions": [
            "not pending in selected run",
            "visible residual/mojibake",
            "scope/concept/gender/domain dependency not yet resolved",
            "insufficient text evidence",
        ],
        "promotion_gate": "Keep read-only until a dominant subtype has its own guarded review; no lifecycle/apply from this broad router.",
        "observed_marker_counts": dict(markers),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only requirement tooltip policy review.")
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    parser.add_argument("--ledger-run-id", required=True, type=int)
    parser.add_argument("--router-jsonl", required=True, type=Path)
    parser.add_argument("--limit", type=int, default=240)
    args = parser.parse_args()

    router_records = read_jsonl(args.router_jsonl)
    route_total = next(
        int(row["segments"])
        for row in router_records
        if row.get("record_type") == "route_count" and row.get("requirement_effect_route") == "requirement_tooltip_policy"
    )

    conn = connect_readonly()
    records, grouped = route_records_from_db(conn, args.segment_state_run_id, args.ledger_run_id)
    sampled = sample_records(records, args.limit)
    text_by_id = fetch_sample_texts(conn, [int(row["segment_id"]) for row in sampled])

    if route_total != len(records):
        raise SystemExit(f"router route count mismatch: jsonl={route_total} reconstructed={len(records)}")

    results: list[dict[str, Any]] = []
    decision_counts: Counter[str] = Counter()
    marker_counts: Counter[str] = Counter()
    next_counts: Counter[str] = Counter()
    apply_later = 0
    lifecycle_later = 0
    for record in sampled:
        segment_id = int(record["segment_id"])
        rows = grouped[segment_id]
        text = text_by_id.get(segment_id, {"old_text": "", "confirmed_text": "", "output_text": ""})
        blob = " ".join([record["blob"], text["old_text"], text["output_text"]])
        tooltip_markers = marker_names(TOOLTIP_MARKERS, blob)
        secondary_markers = marker_names(SECONDARY_MARKERS, blob)
        decision, component, lifecycle, rationale = classify(tooltip_markers + secondary_markers, rows, text)
        if decision not in ALLOWED_DECISIONS:
            raise SystemExit(f"unknown decision {decision} for segment_id {segment_id}")
        requires_apply_later = False
        lifecycle_later += int(lifecycle)
        apply_later += int(requires_apply_later)
        decision_counts[decision] += 1
        marker_counts.update(tooltip_markers + secondary_markers or ["NoMarker"])
        next_counts[component] += 1
        results.append(
            {
                "record_type": "sample_review",
                "segment_id": segment_id,
                "relative_path": record["relative_path"],
                "source_key": record["source_key"],
                "families_open": record["families_open"],
                "cohort_key": record["cohort_key"],
                "old_text": text["old_text"],
                "confirmed_text": text["confirmed_text"],
                "output_text": text["output_text"],
                "tooltip_markers": tooltip_markers,
                "secondary_markers": secondary_markers,
                "requirement_tooltip_decision": decision,
                "next_component": component,
                "requires_lifecycle_later": lifecycle,
                "requires_apply_later": requires_apply_later,
                "corrected_text": "",
                "rationale": rationale,
            }
        )

    if apply_later != 0:
        raise SystemExit(f"requires_apply_later must be 0, got {apply_later}")

    dominant = decision_counts.most_common(1)[0][0] if decision_counts else "none"
    txt_path, jsonl_path, spec_path = output_paths()
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(
            json.dumps(
                {
                    "record_type": "summary",
                    "requirement_tooltip_universe": len(records),
                    "router_jsonl_route_count": route_total,
                    "sampled": len(results),
                    "decision_counts": dict(decision_counts),
                    "marker_counts": dict(marker_counts),
                    "ready_lifecycle_future": lifecycle_later,
                    "apply_candidates_future": apply_later,
                    "dominant_subtype": dominant,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )
        for decision, count in decision_counts.most_common():
            handle.write(json.dumps({"record_type": "decision_count", "requirement_tooltip_decision": decision, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for marker, count in marker_counts.most_common():
            handle.write(json.dumps({"record_type": "marker_count", "marker": marker, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for priority, (component, count) in enumerate(next_counts.most_common(), 1):
            handle.write(json.dumps({"record_type": "component_recommendation", "priority": priority, "component": component, "sampled": count}, ensure_ascii=False, sort_keys=True) + "\n")
        strategy_by_decision = {
            "needs_scope_getter_requirement_policy": ("chat_exec_scope_getter_requirement_policy_review_prompt.md", "dominant sampled subtype inside requirement tooltip"),
            "needs_gender_local_player_requirement_policy": ("chat_exec_gender_local_player_requirement_policy_review_prompt.md", "dominant sampled subtype inside requirement tooltip"),
            "needs_concept_requirement_policy": ("chat_exec_concept_requirement_policy_review_prompt.md", "dominant sampled subtype inside requirement tooltip"),
        }
        primary_strategy = strategy_by_decision.get(
            dominant,
            ("chat_exec_requirement_effect_list_multiline_subpolicy_review_prompt.md", "tooltip remained fragmented; return to second-largest parent route"),
        )
        strategies = [
            primary_strategy,
            ("chat_exec_gender_local_player_requirement_policy_review_prompt.md", "second major tooltip dependency if scope/getter is peeled away first"),
            ("chat_exec_requirement_effect_list_multiline_subpolicy_review_prompt.md", "fallback if tooltip remains fragmented after scope/gender split"),
        ]
        for priority, (next_prompt, rationale) in enumerate(strategies, 1):
            handle.write(json.dumps({"record_type": "strategy", "priority": priority, "next_prompt": next_prompt, "rationale": rationale}, ensure_ascii=False, sort_keys=True) + "\n")
        for row in results:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    with spec_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(build_spec(args.segment_state_run_id, args.ledger_run_id, decision_counts, marker_counts), handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("Requirement tooltip policy review\n\n")
        handle.write(f"universo_requirement_tooltip_policy: {len(records)}\n")
        handle.write(f"router_jsonl_route_count: {route_total}\n")
        handle.write(f"total_amostrado: {len(results)}\n")
        handle.write(f"ready_lifecycle_future: {lifecycle_later}\n")
        handle.write(f"apply_candidates_future: {apply_later}\n")
        handle.write(f"subtipo_dominante: {dominant}\n\n")
        handle.write("requirement_tooltip_decision_counts:\n")
        for decision, count in decision_counts.most_common():
            handle.write(f"- {decision}: {count}\n")
        handle.write("\nTop markers:\n")
        for marker, count in marker_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nTop next components:\n")
        for component, count in next_counts.most_common():
            handle.write(f"- {component}: {count}\n")
        handle.write("\nAnalise\n")
        handle.write("- Requirement tooltip deve ficar antes do parser generico: sim.\n")
        handle.write("- O bloco se comporta como roteador/subpolicy splitter, nao como lifecycle/apply curto.\n")
        handle.write(f"- Subtipo dominante na amostra: {dominant}.\n")
        handle.write(f"- Proximo prompt recomendado: {primary_strategy[0]}.\n")
        handle.write("\nProximos prompts recomendados\n")
        handle.write(f"1. {primary_strategy[0]}\n")
        handle.write("2. chat_exec_gender_local_player_requirement_policy_review_prompt.md\n")
        handle.write("3. chat_exec_requirement_effect_list_multiline_subpolicy_review_prompt.md\n")

    print(f"txt: {txt_path}")
    print(f"jsonl: {jsonl_path}")
    print(f"spec: {spec_path}")
    print(f"requirement_tooltip_universe: {len(records)}")
    print(f"sampled: {len(results)}")
    print(f"ready_lifecycle_future: {lifecycle_later}")
    print(f"apply_candidates_future: {apply_later}")
    print("decision_counts:")
    for decision, count in decision_counts.most_common():
        print(f"  {decision}: {count}")
    print("top_markers:")
    for marker, count in marker_counts.most_common(10):
        print(f"  {marker}: {count}")


if __name__ == "__main__":
    main()
