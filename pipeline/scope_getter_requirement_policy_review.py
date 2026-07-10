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
SOURCE_DECISION = "needs_scope_getter_requirement_policy"

ALLOWED_DECISIONS = {
    "scope_getter_requirement_ready_false_reopen",
    "scope_getter_requirement_ready_lifecycle",
    "needs_root_from_scope_policy",
    "needs_actor_target_scope_policy",
    "needs_recipient_scope_policy",
    "needs_local_player_scope_policy",
    "needs_get_trait_scope_policy",
    "needs_title_law_scope_policy",
    "needs_name_dynasty_scope_policy",
    "needs_concept_scope_policy",
    "needs_script_value_scope_policy",
    "needs_effect_list_scope_policy",
    "needs_domain_context_after_scope",
    "needs_event_context_after_scope",
    "needs_residual_repair_after_scope",
    "needs_dynamic_parser_after_scope",
    "scope_getter_requirement_blocked_uncertain",
}

SCOPE_MARKERS: list[tuple[str, re.Pattern[str]]] = [
    ("ROOT", re.compile(r"\bROOT\.|ROOT\b", re.I)),
    ("FROM", re.compile(r"\bFROM\.|FROM\b", re.I)),
    ("SCOPE", re.compile(r"\bSCOPE\.|scope:", re.I)),
    ("TARGET", re.compile(r"\bTARGET\.|target", re.I)),
    ("CHARACTER", re.compile(r"\bCHARACTER\.|Character\.", re.I)),
    ("Actor", re.compile(r"\bactor\b|GetActor|actor\.", re.I)),
    ("Recipient", re.compile(r"\brecipient\b|\baddressee\b|GetRecipient", re.I)),
    ("LocalPlayer", re.compile(r"GetPlayer|GetLocalPlayer|IsLocalPlayer|local_player", re.I)),
    ("GenericGetter", re.compile(r"Get[A-Za-z0-9_]+\(", re.I)),
]

REQUIREMENT_MARKERS: list[tuple[str, re.Pattern[str]]] = [
    ("Tooltip", re.compile(r"tooltip|_tt\b|#T\b", re.I)),
    ("Requirement", re.compile(r"requirement|required|trigger|valid|allowed|cannot|can_|unlock|available|need|must", re.I)),
    ("Condition", re.compile(r"NO_CHANCE|invalid|valid|blocked|disabled|missing|has_|is_|not_", re.I)),
]

SECONDARY_MARKERS: list[tuple[str, re.Pattern[str]]] = [
    ("GenderLocalPlayer", re.compile(r"Select_CString|ES_(?:OA|XA|EA|ElLa|DelDela|AlAla|A|O)|Get(?:SheHe|HerHis|HerHim|WomanMan|WomenMen)|\bvoc(?:ê|Ãª)\b|\bseu\b|\bsua\b", re.I)),
    ("TraitGetter", re.compile(r"GetTrait|trait|accolade|acclaimed_knight|knight|prowess", re.I)),
    ("TitleLaw", re.compile(r"title|law|government|realm|succession|county|duchy|kingdom|empire|vassal|liege|rank|holding", re.I)),
    ("NameDynasty", re.compile(r"name|nickname|dynasty|house|GetName|GetFirstName|GetDynasty|GetHouse|epithet", re.I)),
    ("Concept", re.compile(r"\[[A-Za-z0-9_]+\|[^\]]+\]|\[Concept\(|Concept\(", re.I)),
    ("ScriptValue", re.compile(r"ScriptValue|GetScriptValue|\|V[0-9]?|\|=\+?0|[0-9]+%", re.I)),
    ("EffectList", re.compile(r"\\n|\n|\$EFFECT_LIST_BULLET\$|#indent|#weak|#bold|#high|#low|#P|#N", re.I)),
    ("Domain", re.compile(r"culture|religion|faith|doctrine|tradition|dynasty|house", re.I)),
    ("Event", re.compile(r"event|\.desc|desc\.|option|toast|dialogue|story|scheme|interaction|memory", re.I)),
    ("ResidualVisible", re.compile(r"NÃƒ|ÃƒÆ’|Ã‚|ï¿½|\b(?:the|your|you|their|cannot|consiguio|consiguiÃ³|sentisteis|sintieron|sera|serÃ¡|mas|mÃ¡s|facil|fÃ¡cil)\b", re.I)),
    ("DynamicToken", re.compile(r"Custom\(|Select_CString|Concept\(|ScriptValue|GetTrait|ROOT\.|FROM\.|SCOPE\.|TARGET\.|\[[^\]]+\]|\$[^$]+\$", re.I)),
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


def source_rows(path: Path) -> list[dict[str, Any]]:
    rows = [
        row
        for row in read_jsonl(path)
        if row.get("record_type") == "sample_review"
        and row.get("requirement_tooltip_decision") == SOURCE_DECISION
    ]
    rows.sort(key=lambda row: (str(row.get("relative_path") or ""), str(row.get("source_key") or ""), int(row["segment_id"])))
    seen: set[int] = set()
    for row in rows:
        segment_id = int(row["segment_id"])
        if segment_id in seen:
            raise SystemExit(f"duplicate source segment_id: {segment_id}")
        seen.add(segment_id)
    return rows


def fetch_states(conn: sqlite3.Connection, run_id: int, segment_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not segment_ids:
        return {}
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT segment_id, final_state, state_group, is_closed, needs_output_apply,
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


def classify(
    row: dict[str, Any],
    state: dict[str, Any] | None,
    scope_markers: list[str],
    requirement_markers: list[str],
    secondary_markers: list[str],
) -> tuple[str, str, bool, str]:
    scope = set(scope_markers)
    req = set(requirement_markers)
    secondary = set(secondary_markers)
    if not state or state["state_group"] != "pending" or int(state["is_closed"] or 0) != 0:
        return "scope_getter_requirement_blocked_uncertain", "human_review_or_evidence_collection", False, "segment is not pending in selected state run"
    if int(state["needs_output_apply"] or 0) != 0 or int(state["confirmed_matches_output"] or 0) != 1:
        return "scope_getter_requirement_blocked_uncertain", "state_guard", False, "state guard failed: needs_output_apply or confirmed_matches_output"
    if "ResidualVisible" in secondary:
        return "needs_residual_repair_after_scope", "residual_dependency_filtered_repair", False, "visible residual/mojibake remains"
    if "GenderLocalPlayer" in secondary or "LocalPlayer" in scope:
        return "needs_local_player_scope_policy", "local_player_scope_policy", False, "scope/getter depends on local-player perspective"
    if "Recipient" in scope:
        return "needs_recipient_scope_policy", "recipient_scope_policy", False, "recipient/addressee scope is explicit"
    if "Actor" in scope or "TARGET" in scope:
        return "needs_actor_target_scope_policy", "actor_target_scope_policy", False, "actor/target scope is explicit"
    if "ScriptValue" in secondary:
        return "needs_script_value_scope_policy", "script_value_scope_policy", False, "script value is mixed with scope/getter requirement"
    if "TraitGetter" in secondary:
        return "needs_get_trait_scope_policy", "get_trait_scope_policy", False, "trait/accolade getter is mixed with scope/getter requirement"
    if "TitleLaw" in secondary:
        return "needs_title_law_scope_policy", "title_law_scope_policy", False, "title/law/government scope vocabulary is present"
    if "NameDynasty" in secondary:
        return "needs_name_dynasty_scope_policy", "name_dynasty_scope_policy", False, "name/dynasty getter remains after scope routing"
    if "Concept" in secondary:
        return "needs_concept_scope_policy", "concept_scope_policy", False, "concept expression is mixed with scope/getter requirement"
    if "EffectList" in secondary:
        return "needs_effect_list_scope_policy", "effect_list_scope_policy", False, "effect-list/multiline surface is mixed with scope/getter requirement"
    if "ROOT" in scope or "FROM" in scope or "SCOPE" in scope or "CHARACTER" in scope or "GenericGetter" in scope:
        return "needs_root_from_scope_policy", "root_from_scope_policy", False, "generic ROOT/FROM/SCOPE/getter policy is needed"
    if "Domain" in secondary:
        return "needs_domain_context_after_scope", "domain_context_composer", False, "domain context remains after scope routing"
    if "Event" in secondary:
        return "needs_event_context_after_scope", "event_context_composer", False, "event context remains after scope routing"
    if "DynamicToken" in secondary:
        return "needs_dynamic_parser_after_scope", "ck3_dynamic_symbolic_parser", False, "dynamic token remains after scope routing"
    if req and int(state["confirmed_matches_output"] or 0) == 1:
        if int(state["needs_reopen"] or 0) == 1:
            return "scope_getter_requirement_ready_false_reopen", "false_reopen_lifecycle_bridge", True, "plain scope tooltip may be a future false-reopen candidate"
        return "scope_getter_requirement_ready_lifecycle", "scope_requirement_lifecycle_bridge", True, "plain scope tooltip may be a future lifecycle candidate"
    return "scope_getter_requirement_blocked_uncertain", "human_review_or_evidence_collection", False, "insufficient marker evidence"


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_scope_getter_requirement_policy_review"
    spec = reports_dir / f"{stamp}_scope_getter_requirement_policy_spec.json"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), spec


def build_spec(run_id: int, decisions: Counter[str], scope_counts: Counter[str], secondary_counts: Counter[str]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "created_for": "read_only_subpolicy_design",
        "parent_policy": "requirement_tooltip_policy",
        "policy_id": "scope_getter_requirement_policy",
        "segment_state_run_id": run_id,
        "ledger_run_id": LEDGER_RUN_ID,
        "entry_conditions": [
            "requirement_tooltip_decision == needs_scope_getter_requirement_policy",
            "segment remains pending",
            "needs_output_apply == 0",
            "confirmed_matches_output == 1",
        ],
        "scope_types": [{"scope_type": marker, "sampled": count} for marker, count in scope_counts.most_common()],
        "resolution_order": [
            "state guard",
            "residual visible guard",
            "local player/gender scope",
            "recipient/addressee scope",
            "actor/target scope",
            "script value, trait, title, name, concept, effect-list overlays",
            "ROOT/FROM/SCOPE/generic getter",
            "domain/event/dynamic handoff",
        ],
        "next_components": [
            "local_player_scope_policy",
            "recipient_scope_policy",
            "actor_target_scope_policy",
            "script_value_scope_policy",
            "get_trait_scope_policy",
            "title_law_scope_policy",
            "name_dynasty_scope_policy",
            "concept_scope_policy",
            "effect_list_scope_policy",
            "root_from_scope_policy",
            "domain_context_composer",
            "event_context_composer",
            "ck3_dynamic_symbolic_parser",
        ],
        "blocked_conditions": [
            "state guard failed",
            "visible residual/mojibake",
            "ambiguous scope marker stack",
            "missing marker evidence",
        ],
        "promotion_gate": "Keep read-only until the dominant scope subtype is reviewed separately; no lifecycle/apply in this broad subpolicy.",
        "observed_decision_counts": dict(decisions),
        "observed_secondary_marker_counts": dict(secondary_counts),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only scope/getter requirement policy review.")
    parser.add_argument("--tooltip-jsonl", required=True, type=Path)
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    args = parser.parse_args()

    rows = source_rows(args.tooltip_jsonl)
    conn = connect_readonly()
    states = fetch_states(conn, args.segment_state_run_id, [int(row["segment_id"]) for row in rows])

    results: list[dict[str, Any]] = []
    decision_counts: Counter[str] = Counter()
    scope_counts: Counter[str] = Counter()
    req_counts: Counter[str] = Counter()
    secondary_counts: Counter[str] = Counter()
    next_counts: Counter[str] = Counter()
    lifecycle_later = 0
    apply_later = 0
    pending_count = 0

    for row in rows:
        segment_id = int(row["segment_id"])
        state = states.get(segment_id)
        if state and state["state_group"] == "pending" and int(state["is_closed"] or 0) == 0:
            pending_count += 1
        blob = " ".join(
            str(row.get(key) or "")
            for key in ("relative_path", "source_key", "old_text", "confirmed_text", "output_text")
        )
        scope_markers = markers(SCOPE_MARKERS, blob)
        requirement_markers = markers(REQUIREMENT_MARKERS, blob)
        secondary_markers = markers(SECONDARY_MARKERS, blob)
        decision, component, lifecycle, rationale = classify(row, state, scope_markers, requirement_markers, secondary_markers)
        if decision not in ALLOWED_DECISIONS:
            raise SystemExit(f"unknown decision {decision} for segment_id {segment_id}")
        requires_apply_later = False
        lifecycle_later += int(lifecycle)
        apply_later += int(requires_apply_later)
        decision_counts[decision] += 1
        scope_counts.update(scope_markers or ["NoScopeMarker"])
        req_counts.update(requirement_markers or ["NoRequirementMarker"])
        secondary_counts.update(secondary_markers or ["NoSecondaryMarker"])
        next_counts[component] += 1
        results.append(
            {
                "record_type": "sample_review",
                "segment_id": segment_id,
                "relative_path": str(row.get("relative_path") or ""),
                "source_key": str(row.get("source_key") or ""),
                "families_open": list(row.get("families_open") or []),
                "source_decision": SOURCE_DECISION,
                "old_text": str(row.get("old_text") or ""),
                "confirmed_text": str(row.get("confirmed_text") or ""),
                "output_text": str(row.get("output_text") or ""),
                "scope_markers": scope_markers,
                "requirement_markers": requirement_markers,
                "secondary_markers": secondary_markers,
                "scope_getter_requirement_decision": decision,
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
    if dominant == "needs_local_player_scope_policy":
        next_prompt = "chat_exec_local_player_scope_requirement_policy_review_prompt.md"
    elif dominant == "needs_actor_target_scope_policy":
        next_prompt = "chat_exec_actor_target_scope_requirement_policy_review_prompt.md"
    elif dominant == "needs_script_value_scope_policy":
        next_prompt = "chat_exec_script_value_scope_requirement_policy_review_prompt.md"
    elif dominant == "needs_concept_scope_policy":
        next_prompt = "chat_exec_concept_scope_requirement_policy_review_prompt.md"
    elif dominant == "needs_effect_list_scope_policy":
        next_prompt = "chat_exec_effect_list_scope_requirement_policy_review_prompt.md"
    elif dominant == "needs_get_trait_scope_policy":
        next_prompt = "chat_exec_get_trait_scope_requirement_policy_review_prompt.md"
    elif dominant == "needs_name_dynasty_scope_policy":
        next_prompt = "chat_exec_name_dynasty_scope_requirement_policy_review_prompt.md"
    elif dominant == "needs_title_law_scope_policy":
        next_prompt = "chat_exec_title_law_scope_requirement_policy_review_prompt.md"
    else:
        next_prompt = "chat_exec_root_from_scope_requirement_policy_review_prompt.md"

    txt_path, jsonl_path, spec_path = output_paths()
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(
            json.dumps(
                {
                    "record_type": "summary",
                    "source_decision": SOURCE_DECISION,
                    "total_reviewed": len(results),
                    "pending_count": pending_count,
                    "decision_counts": dict(decision_counts),
                    "scope_marker_counts": dict(scope_counts),
                    "requirement_marker_counts": dict(req_counts),
                    "secondary_marker_counts": dict(secondary_counts),
                    "ready_lifecycle_future": lifecycle_later,
                    "apply_candidates_future": apply_later,
                    "dominant_subtype": dominant,
                    "next_prompt": next_prompt,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )
        for decision, count in decision_counts.most_common():
            handle.write(json.dumps({"record_type": "decision_count", "scope_getter_requirement_decision": decision, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for marker, count in scope_counts.most_common():
            handle.write(json.dumps({"record_type": "scope_marker_count", "marker": marker, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for marker, count in secondary_counts.most_common():
            handle.write(json.dumps({"record_type": "secondary_marker_count", "marker": marker, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        strategies = [
            (next_prompt, "dominant subtype from scope/getter requirement review"),
            ("chat_exec_gender_local_player_requirement_policy_review_prompt.md", "parallel tooltip dependency still has high volume"),
            ("chat_exec_requirement_effect_list_multiline_subpolicy_review_prompt.md", "fallback second-largest parent route if scope remains fragmented"),
        ]
        for priority, (prompt, rationale) in enumerate(strategies, 1):
            handle.write(json.dumps({"record_type": "strategy", "priority": priority, "next_prompt": prompt, "rationale": rationale}, ensure_ascii=False, sort_keys=True) + "\n")
        for result in results:
            handle.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")

    with spec_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(build_spec(args.segment_state_run_id, decision_counts, scope_counts, secondary_counts), handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("Scope/getter requirement policy review\n\n")
        handle.write(f"total_revisado: {len(results)}\n")
        handle.write(f"pending_count: {pending_count}\n")
        handle.write(f"ready_lifecycle_future: {lifecycle_later}\n")
        handle.write(f"apply_candidates_future: {apply_later}\n")
        handle.write(f"subtipo_dominante: {dominant}\n\n")
        handle.write("scope_getter_requirement_decision_counts:\n")
        for decision, count in decision_counts.most_common():
            handle.write(f"- {decision}: {count}\n")
        handle.write("\nTop scope markers:\n")
        for marker, count in scope_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nTop secondary markers:\n")
        for marker, count in secondary_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nAnalise\n")
        handle.write("- scope_getter_requirement_policy deve virar componente read-only real: sim, como splitter antes do parser generico.\n")
        handle.write("- Lifecycle/apply curto: nao; zero candidatos nesta revisao.\n")
        handle.write(f"- Subtipo dominante: {dominant}.\n")
        handle.write(f"- Proximo prompt recomendado: {next_prompt}.\n")
        handle.write("- A policy ainda fragmenta em subtipos de perspectiva/dominio; nao deve aplicar diretamente.\n")
        handle.write("\nProximos prompts recomendados\n")
        handle.write(f"1. {next_prompt}\n")
        handle.write("2. chat_exec_gender_local_player_requirement_policy_review_prompt.md\n")
        handle.write("3. chat_exec_requirement_effect_list_multiline_subpolicy_review_prompt.md\n")

    print(f"txt: {txt_path}")
    print(f"jsonl: {jsonl_path}")
    print(f"spec: {spec_path}")
    print(f"total_reviewed: {len(results)}")
    print(f"pending_count: {pending_count}")
    print(f"ready_lifecycle_future: {lifecycle_later}")
    print(f"apply_candidates_future: {apply_later}")
    print("decision_counts:")
    for decision, count in decision_counts.most_common():
        print(f"  {decision}: {count}")
    print("top_scope_markers:")
    for marker, count in scope_counts.most_common(10):
        print(f"  {marker}: {count}")


if __name__ == "__main__":
    main()
