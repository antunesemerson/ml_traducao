from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


LEDGER_RUN_ID = 76
SOURCE_DECISION = "needs_concept_requirement_policy"

ALLOWED_DECISIONS = {
    "concept_requirement_terminal_policy",
    "concept_requirement_terminal_policy_with_domain_guard",
    "concept_requirement_terminal_policy_with_scope_guard",
    "concept_requirement_terminal_policy_with_effect_guard",
    "needs_concept_requirement_domain_policy",
    "needs_concept_requirement_scope_getter_policy",
    "needs_concept_requirement_effect_list_policy",
    "needs_concept_requirement_script_value_policy",
    "needs_concept_requirement_title_law_policy",
    "needs_concept_requirement_trait_accolade_policy",
    "needs_concept_requirement_name_dynasty_policy",
    "needs_concept_requirement_gender_local_player_policy",
    "needs_concept_requirement_event_context",
    "needs_concept_requirement_residual_repair",
    "needs_concept_requirement_dynamic_parser_escape",
    "concept_requirement_blocked_uncertain",
}

CONCEPT_MARKERS = [
    ("ConceptLink", re.compile(r"\[[a-zA-Z0-9_]+\|[lE]+\]", re.I)),
    ("ConceptFunction", re.compile(r"Concept\(", re.I)),
    ("CultureConcept", re.compile(r"\[culture\|[lE]+\]|\[tradition[s]?\|[lE]+\]|\[culture_pillar\|[lE]+\]", re.I)),
    ("BuildingConcept", re.compile(r"\[building[s]?\|[lE]+\]|\[holdings\|[lE]+\]|\[counties\|[lE]+\]", re.I)),
    ("AccoladeConcept", re.compile(r"\[accolade|accolade_types", re.I)),
]

REQUIREMENT_MARKERS = [
    ("Cannot", re.compile(r"não pode|incompatível|não tem|não possui|nao pode|incompativel", re.I)),
    ("HasRequirement", re.compile(r"tem|possui|desbloqueado|unlock|pode|sempre|exigem", re.I)),
    ("Parameter", re.compile(r"culture_parameter|HAS_CULTURAL|LACKS_OVERLAPPING|cannot_", re.I)),
]

GUARD_MARKERS = [
    ("DomainGuard", re.compile(r"culture|tradition|culture_tradition|culture_pillar|building|innovation|tax_decree|terrain|region|legend", re.I)),
    ("ScopeGuard", re.compile(r"CULTURE\.|CULTURE_TRADITION\.|CULTURE_PILLAR\.|ROOT\.|FROM\.|TARGET\.|GetName", re.I)),
    ("EffectGuard", re.compile(r"\\n|\n|\$EFFECT_LIST_BULLET\$|#indent|#weak|#bold|#high|#low|#P|#N", re.I)),
    ("GenderLocalPlayerGuard", re.compile(r"Select_CString|SelectLocalization|IsLocalPlayer|ES_OA|ES_XA|ES_ElLa|ES_DelDela", re.I)),
]

SECONDARY_MARKERS = [
    ("ScopeGetter", re.compile(r"CULTURE\.|CULTURE_TRADITION\.|CULTURE_PILLAR\.|ROOT\.|FROM\.|TARGET\.|Get[A-Za-z0-9_]+\(", re.I)),
    ("EffectList", re.compile(r"\\n|\n|\$EFFECT_LIST_BULLET\$|#indent|#weak|#bold|#high|#low|#P|#N", re.I)),
    ("ScriptValue", re.compile(r"ScriptValue|GetScriptValue|\$VALUE|[0-9]+%", re.I)),
    ("TitleLaw", re.compile(r"title|law|government|realm|landed|tax_decree", re.I)),
    ("TraitAccolade", re.compile(r"trait|accolade|knight", re.I)),
    ("NameDynasty", re.compile(r"name|nickname|dynasty|house|GetName", re.I)),
    ("Domain", re.compile(r"culture|tradition|building|innovation|terrain|region|legend|tax|parameter", re.I)),
    ("Event", re.compile(r"event|\.desc|desc\.|option|toast|dialogue|story|scheme|interaction|memory|battle", re.I)),
    ("ResidualVisible", re.compile(r"Ãƒ|Ã‚|Â¿|Â¡|â€™|â€œ|â€�|�|\bthe\b|\byour\b|\byou\b|\btheir\b|\bcannot\b", re.I)),
    ("DynamicToken", re.compile(r"Custom\(|Select_CString|SelectLocalization|Concept\(|ScriptValue|GetTrait|ROOT\.|FROM\.|SCOPE\.|TARGET\.|\[[^\]]+\]|\$[^$]+\$", re.I)),
]


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"Invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def source_rows(path: Path) -> list[dict[str, Any]]:
    rows = [
        row
        for row in read_jsonl(path)
        if row.get("record_type") == "sample_review"
        and row.get("requirement_tooltip_decision") == SOURCE_DECISION
    ]
    rows.sort(key=lambda row: (str(row.get("relative_path") or ""), str(row.get("source_key") or ""), int(row["segment_id"])))
    seen = set()
    for row in rows:
        segment_id = int(row["segment_id"])
        if segment_id in seen:
            raise SystemExit(f"duplicate source segment_id: {segment_id}")
        seen.add(segment_id)
    return rows


def fetch_states(conn: sqlite3.Connection, run_id: int, segment_ids: list[int]) -> dict[int, dict[str, Any]]:
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT segment_id, state_group, is_closed, needs_output_apply,
               confirmed_matches_output, needs_reopen
        FROM segment_state_items
        WHERE run_id = ?
          AND segment_id IN ({placeholders})
        """,
        (run_id, *segment_ids),
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def markers(patterns: list[tuple[str, re.Pattern[str]]], blob: str) -> list[str]:
    return [label for label, pattern in patterns if pattern.search(blob)]


def classify(state: dict[str, Any] | None, guards: list[str], secondary: list[str]) -> tuple[str, str, str]:
    guard_set = set(guards)
    secondary_set = set(secondary)
    if not state or state["state_group"] != "pending" or int(state["is_closed"] or 0) != 0:
        return "concept_requirement_blocked_uncertain", "human_review_or_evidence_collection", "segment is not pending in selected state run"
    if int(state["needs_output_apply"] or 0) != 0 or int(state["confirmed_matches_output"] or 0) != 1:
        return "concept_requirement_blocked_uncertain", "state_guard", "state guard failed: needs_output_apply or confirmed_matches_output"
    if "ResidualVisible" in secondary_set:
        return "needs_concept_requirement_residual_repair", "residual_dependency_filtered_repair", "visible residual remains"
    if "ScriptValue" in secondary_set:
        return "needs_concept_requirement_script_value_policy", "script_value_requirement_policy", "ScriptValue dominates concept requirement"
    if "GenderLocalPlayerGuard" in guard_set:
        return "needs_concept_requirement_gender_local_player_policy", "gender_local_player_requirement_policy", "gender/local-player dynamic dominates concept requirement"
    if "EffectList" in secondary_set or "EffectGuard" in guard_set:
        return "concept_requirement_terminal_policy_with_effect_guard", "terminal_router_policy", "concept requirement with effect/list formatting guard"
    if "ScopeGuard" in guard_set:
        return "concept_requirement_terminal_policy_with_scope_guard", "terminal_router_policy", "concept requirement with scope/getter guard"
    if "DomainGuard" in guard_set or "Domain" in secondary_set:
        return "concept_requirement_terminal_policy_with_domain_guard", "terminal_router_policy", "concept requirement with strong domain guard"
    if "TitleLaw" in secondary_set:
        return "needs_concept_requirement_title_law_policy", "concept_title_law_policy", "title/law domain dominates concept requirement"
    if "TraitAccolade" in secondary_set:
        return "needs_concept_requirement_trait_accolade_policy", "concept_trait_accolade_policy", "trait/accolade domain dominates concept requirement"
    if "NameDynasty" in secondary_set:
        return "needs_concept_requirement_name_dynasty_policy", "concept_name_dynasty_policy", "name/dynasty getter dominates concept requirement"
    if "Event" in secondary_set:
        return "needs_concept_requirement_event_context", "event_context_composer", "event context dominates"
    if "DynamicToken" in secondary_set:
        return "needs_concept_requirement_dynamic_parser_escape", "ck3_dynamic_symbolic_parser", "dynamic parser escape remains"
    return "concept_requirement_terminal_policy", "terminal_router_policy", "plain concept requirement terminal policy"


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_concept_requirement_policy_review"
    spec = reports_dir / f"{stamp}_concept_requirement_policy_spec.json"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), spec


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only concept requirement policy review.")
    parser.add_argument("--tooltip-jsonl", required=True, type=Path)
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    args = parser.parse_args()

    rows = source_rows(args.tooltip_jsonl)
    conn = connect_readonly()
    states = fetch_states(conn, args.segment_state_run_id, [int(row["segment_id"]) for row in rows])

    results = []
    decision_counts: Counter[str] = Counter()
    concept_counts: Counter[str] = Counter()
    requirement_counts: Counter[str] = Counter()
    guard_counts: Counter[str] = Counter()
    secondary_counts: Counter[str] = Counter()
    pending_count = 0

    for row in rows:
        segment_id = int(row["segment_id"])
        state = states.get(segment_id)
        if state and state["state_group"] == "pending" and int(state["is_closed"] or 0) == 0:
            pending_count += 1
        blob = " ".join(str(row.get(key) or "") for key in ("relative_path", "source_key", "old_text", "confirmed_text", "output_text"))
        concept_markers = markers(CONCEPT_MARKERS, blob)
        requirement_markers = markers(REQUIREMENT_MARKERS, blob)
        guard_markers = markers(GUARD_MARKERS, blob)
        secondary_markers = markers(SECONDARY_MARKERS, blob)
        decision, next_component, rationale = classify(state, guard_markers, secondary_markers)
        if decision not in ALLOWED_DECISIONS:
            raise SystemExit(f"unknown decision {decision} for segment_id {segment_id}")
        decision_counts[decision] += 1
        concept_counts.update(concept_markers or ["NoConceptMarker"])
        requirement_counts.update(requirement_markers or ["NoRequirementMarker"])
        guard_counts.update(guard_markers or ["NoGuardMarker"])
        secondary_counts.update(secondary_markers or ["NoSecondaryMarker"])
        results.append({
            "record_type": "sample_review",
            "segment_id": segment_id,
            "relative_path": str(row.get("relative_path") or ""),
            "source_key": str(row.get("source_key") or ""),
            "families_open": list(row.get("families_open") or []),
            "source_decision": SOURCE_DECISION,
            "old_text": str(row.get("old_text") or ""),
            "confirmed_text": str(row.get("confirmed_text") or ""),
            "output_text": str(row.get("output_text") or ""),
            "concept_markers": concept_markers,
            "requirement_markers": requirement_markers,
            "guard_markers": guard_markers,
            "secondary_markers": secondary_markers,
            "concept_requirement_decision": decision,
            "next_component": next_component,
            "requires_lifecycle_later": False,
            "requires_apply_later": False,
            "corrected_text": "",
            "rationale": rationale,
        })

    terminal_count = sum(count for decision, count in decision_counts.items() if decision.startswith("concept_requirement_terminal_policy"))
    dominant, dominant_count = decision_counts.most_common(1)[0] if decision_counts else ("none", 0)
    if dominant.startswith("needs_") and dominant_count >= 10:
        next_prompt = f"chat_exec_{dominant.removeprefix('needs_')}_review_prompt.md"
        stop_rule = f"continue_narrow_prompt: {dominant} reached {dominant_count} >= 10"
    elif terminal_count > len(results) / 2:
        next_prompt = "chat_exec_name_nickname_requirement_guard_review_prompt.md"
        stop_rule = "terminal_majority_return_to_name_nickname_guard"
    else:
        next_prompt = "chat_exec_name_nickname_requirement_guard_review_prompt.md"
        stop_rule = "fragmented_return_to_name_nickname_guard"

    spec = {
        "schema_version": 1,
        "created_for": "read_only_subpolicy_design",
        "parent_policy": "requirement_tooltip_policy",
        "policy_id": "concept_requirement_policy",
        "segment_state_run_id": args.segment_state_run_id,
        "ledger_run_id": LEDGER_RUN_ID,
        "entry_conditions": [
            "requirement_tooltip_decision == needs_concept_requirement_policy",
            "segment remains pending",
            "needs_output_apply == 0",
            "confirmed_matches_output == 1",
        ],
        "concept_requirement_types": [{"decision": decision, "sampled": count} for decision, count in decision_counts.most_common()],
        "resolution_order": [
            "state guard",
            "residual guard",
            "ScriptValue and gender/local-player escapes",
            "effect/list guard",
            "scope/getter guard",
            "domain concept terminal policy",
            "title/law, trait/accolade, name/dynasty fallbacks",
        ],
        "next_components": [
            "concept_requirement_domain_policy",
            "concept_requirement_scope_getter_policy",
            "concept_requirement_effect_list_policy",
            "script_value_requirement_policy",
            "gender_local_player_requirement_policy",
            "name_nickname_requirement_guard",
        ],
        "blocked_conditions": ["state guard failed", "visible residual", "ambiguous concept/dynamic surface"],
        "promotion_gate": "read_only_component_only_no_apply_no_lifecycle",
        "terminal_policy_majority": terminal_count > len(results) / 2,
        "stop_rule": stop_rule,
    }

    txt_path, jsonl_path, spec_path = output_paths()
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps({
            "record_type": "summary",
            "source_decision": SOURCE_DECISION,
            "total_reviewed": len(results),
            "pending_count": pending_count,
            "decision_counts": dict(decision_counts),
            "concept_marker_counts": dict(concept_counts),
            "requirement_marker_counts": dict(requirement_counts),
            "guard_marker_counts": dict(guard_counts),
            "secondary_marker_counts": dict(secondary_counts),
            "ready_lifecycle_future": 0,
            "apply_candidates_future": 0,
            "dominant_subtype": dominant,
            "dominant_count": dominant_count,
            "terminal_policy_majority": terminal_count > len(results) / 2,
            "stop_rule": stop_rule,
            "next_prompt": next_prompt,
        }, ensure_ascii=False, sort_keys=True) + "\n")
        for decision, count in decision_counts.most_common():
            handle.write(json.dumps({"record_type": "decision_count", "concept_requirement_decision": decision, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for marker, count in concept_counts.most_common():
            handle.write(json.dumps({"record_type": "concept_marker_count", "marker": marker, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for marker, count in requirement_counts.most_common():
            handle.write(json.dumps({"record_type": "requirement_marker_count", "marker": marker, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for marker, count in guard_counts.most_common():
            handle.write(json.dumps({"record_type": "guard_marker_count", "marker": marker, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for marker, count in secondary_counts.most_common():
            handle.write(json.dumps({"record_type": "secondary_marker_count", "marker": marker, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for priority, (prompt, rationale) in enumerate([
            (next_prompt, "selected by concept requirement stop rule"),
            ("chat_exec_global_next_focus_after_requirement_tooltip_prompt.md", "diagnostic option after terminal policy consolidation"),
            ("chat_exec_requirement_effect_router_component_integration_review_prompt.md", "architecture/integration option"),
        ], 1):
            handle.write(json.dumps({"record_type": "strategy", "priority": priority, "next_prompt": prompt, "rationale": rationale}, ensure_ascii=False, sort_keys=True) + "\n")
        for result in results:
            handle.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")

    with spec_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(spec, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("Concept requirement policy review\n\n")
        handle.write(f"total_revisado: {len(results)}\n")
        handle.write(f"pending_count: {pending_count}\n")
        handle.write("ready_lifecycle_future: 0\n")
        handle.write("apply_candidates_future: 0\n")
        handle.write(f"subtipo_dominante: {dominant}\n")
        handle.write(f"dominant_count: {dominant_count}\n")
        handle.write(f"regra_de_parada: {stop_rule}\n")
        handle.write(f"proximo_prompt: {next_prompt}\n\n")
        handle.write("concept_requirement_decision_counts:\n")
        for decision, count in decision_counts.most_common():
            handle.write(f"- {decision}: {count}\n")
        handle.write("\nTop concept markers:\n")
        for marker, count in concept_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nTop requirement markers:\n")
        for marker, count in requirement_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nTop guard markers:\n")
        for marker, count in guard_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nTop secondary markers:\n")
        for marker, count in secondary_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nAnalise\n")
        handle.write("- concept_requirement_policy deve virar componente read-only real.\n")
        handle.write("- Requirement tooltip deve continuar antes do parser generico para estas superficies.\n")
        handle.write("- Esta revisao nao gera lifecycle/apply.\n")

    print(f"txt: {txt_path}")
    print(f"jsonl: {jsonl_path}")
    print(f"spec: {spec_path}")
    print(f"total_reviewed: {len(results)}")
    print(f"pending_count: {pending_count}")
    print("ready_lifecycle_future: 0")
    print("apply_candidates_future: 0")
    print(f"stop_rule: {stop_rule}")
    print("decision_counts:")
    for decision, count in decision_counts.most_common():
        print(f"  {decision}: {count}")


if __name__ == "__main__":
    main()
