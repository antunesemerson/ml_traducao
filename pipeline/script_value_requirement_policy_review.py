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
SOURCE_DECISION = "needs_script_value_requirement_policy"

ALLOWED_DECISIONS = {
    "script_value_terminal_policy",
    "script_value_terminal_policy_with_numeric_guard",
    "script_value_terminal_policy_with_scope_guard",
    "script_value_terminal_policy_with_domain_guard",
    "needs_script_value_numeric_modifier_policy",
    "needs_script_value_percent_modifier_policy",
    "needs_script_value_title_law_policy",
    "needs_script_value_trait_accolade_policy",
    "needs_script_value_scope_getter_policy",
    "needs_script_value_concept_policy",
    "needs_script_value_effect_list_policy",
    "needs_script_value_name_dynasty_policy",
    "needs_script_value_domain_context",
    "needs_script_value_event_context",
    "needs_script_value_residual_repair",
    "needs_script_value_dynamic_parser_escape",
    "script_value_blocked_uncertain",
}

SCRIPT_VALUE_MARKERS = [
    ("ScopeScriptValue", re.compile(r"SCOPE\.ScriptValue", re.I)),
    ("GetScriptValue", re.compile(r"GetScriptValue", re.I)),
    ("ValueToken", re.compile(r"\$VALUE\|V?[0-9]?\$", re.I)),
    ("ScriptValueSalary", re.compile(r"salary|salário|salario|court_position_.*salary|_salary'\)", re.I)),
]

NUMERIC_MARKERS = [
    ("ValuePrecision", re.compile(r"\|[V]?[0-9]\]|\|V0\$|\|2\]", re.I)),
    ("CurrencyIcon", re.compile(r"\[gold_i\]|\[prestige_i\]", re.I)),
    ("PercentOrModifier", re.compile(r"%|percent|modifier|GetModifier|bonus|penalty", re.I)),
    ("NumericLiteral", re.compile(r"\b[0-9]+\b", re.I)),
]

REQUIREMENT_MARKERS = [
    ("Tooltip", re.compile(r"tooltip|_tt\b|#T\b", re.I)),
    ("Requirement", re.compile(r"trigger|valid|unlock|parameter|desbloqueia|required|requisito|salary|salário|salario", re.I)),
    ("Operator", re.compile(r"\$OPERATOR\$|\$VALUE", re.I)),
]

SECONDARY_MARKERS = [
    ("ScopeGetter", re.compile(r"SCOPE\.|ROOT\.|FROM\.|TARGET\.|TITLE\.|TARGET_TITLE\.|Get[A-Za-z0-9_]+\(", re.I)),
    ("TitleLaw", re.compile(r"TITLE\.|TARGET_TITLE\.|de_jure|law|government|realm|title", re.I)),
    ("TraitAccolade", re.compile(r"trait|accolade|knight|court_position|camp_officer", re.I)),
    ("Concept", re.compile(r"\[[a-zA-Z0-9_]+\|[lE]+\]|Concept\(", re.I)),
    ("EffectList", re.compile(r"\\n|\n|\$EFFECT_LIST_BULLET\$|#indent|#weak|#bold|#high|#low|#P|#N", re.I)),
    ("NameDynasty", re.compile(r"name|nickname|dynasty|house|GetName", re.I)),
    ("Domain", re.compile(r"court|position|salary|gold|prestige|culture|building|innovation|drift|domain", re.I)),
    ("Event", re.compile(r"event|\.desc|desc\.|option|toast|dialogue|story|scheme|interaction|memory|battle|event_localization", re.I)),
    ("ResidualVisible", re.compile(r"Ãƒ|Ã‚|Â¿|Â¡|â€™|â€œ|â€�|�|\bdireçăo\b|\bthe\b|\byour\b|\byou\b|\btheir\b|\bcannot\b", re.I)),
    ("DynamicToken", re.compile(r"Custom\(|Select_CString|Concept\(|ScriptValue|GetTrait|ROOT\.|FROM\.|SCOPE\.|TARGET\.|\[[^\]]+\]|\$[^$]+\$", re.I)),
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


def normalize(text: str) -> str:
    return text.replace("salário", "salario").replace("direção", "direcao")


def markers(patterns: list[tuple[str, re.Pattern[str]]], blob: str) -> list[str]:
    normalized = normalize(blob)
    return [label for label, pattern in patterns if pattern.search(normalized)]


def classify(
    state: dict[str, Any] | None,
    script_markers: list[str],
    numeric_markers: list[str],
    secondary_markers: list[str],
) -> tuple[str, str, str]:
    script = set(script_markers)
    numeric = set(numeric_markers)
    secondary = set(secondary_markers)
    if not state or state["state_group"] != "pending" or int(state["is_closed"] or 0) != 0:
        return "script_value_blocked_uncertain", "human_review_or_evidence_collection", "segment is not pending in selected state run"
    if int(state["needs_output_apply"] or 0) != 0 or int(state["confirmed_matches_output"] or 0) != 1:
        return "script_value_blocked_uncertain", "state_guard", "state guard failed: needs_output_apply or confirmed_matches_output"
    if "ResidualVisible" in secondary:
        return "needs_script_value_residual_repair", "residual_dependency_filtered_repair", "visible residual remains"
    if "EffectList" in secondary:
        return "needs_script_value_effect_list_policy", "script_value_effect_list_policy", "effect-list/multiline dominates script value case"
    if "ScopeScriptValue" in script and ("ValuePrecision" in numeric or "CurrencyIcon" in numeric):
        return "script_value_terminal_policy_with_numeric_guard", "terminal_router_policy", "ScriptValue salary/value tooltip with numeric formatting guard"
    if "PercentOrModifier" in numeric:
        return "needs_script_value_percent_modifier_policy", "script_value_percent_modifier_policy", "percent/modifier dependency dominates"
    if "ValueToken" in script and "TitleLaw" in secondary:
        return "needs_script_value_title_law_policy", "script_value_title_law_policy", "title/law comparator value remains"
    if "TraitAccolade" in secondary and "ScopeScriptValue" not in script:
        return "needs_script_value_trait_accolade_policy", "script_value_trait_accolade_policy", "trait/accolade dependency without ScriptValue dominates"
    if "ScopeGetter" in secondary and script:
        return "script_value_terminal_policy_with_scope_guard", "terminal_router_policy", "ScriptValue remains terminal with scope/getter guard"
    if "Concept" in secondary and "ScopeScriptValue" not in script:
        return "needs_script_value_concept_policy", "script_value_concept_policy", "concept expression dominates"
    if "NameDynasty" in secondary and "ScopeScriptValue" not in script:
        return "needs_script_value_name_dynasty_policy", "script_value_name_dynasty_policy", "name/dynasty getter dominates"
    if "Domain" in secondary:
        return "script_value_terminal_policy_with_domain_guard", "terminal_router_policy", "terminal ScriptValue-like requirement with domain guard"
    if "Event" in secondary:
        return "needs_script_value_event_context", "event_context_composer", "event context dominates"
    if "DynamicToken" in secondary:
        return "needs_script_value_dynamic_parser_escape", "ck3_dynamic_symbolic_parser", "dynamic parser escape remains"
    if script:
        return "script_value_terminal_policy", "terminal_router_policy", "plain terminal ScriptValue requirement policy"
    return "script_value_blocked_uncertain", "human_review_or_evidence_collection", "insufficient ScriptValue marker evidence"


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_script_value_requirement_policy_review"
    spec = reports_dir / f"{stamp}_script_value_requirement_policy_spec.json"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), spec


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only ScriptValue requirement policy review.")
    parser.add_argument("--tooltip-jsonl", required=True, type=Path)
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    args = parser.parse_args()

    rows = source_rows(args.tooltip_jsonl)
    conn = connect_readonly()
    states = fetch_states(conn, args.segment_state_run_id, [int(row["segment_id"]) for row in rows])

    results = []
    decision_counts: Counter[str] = Counter()
    script_counts: Counter[str] = Counter()
    numeric_counts: Counter[str] = Counter()
    requirement_counts: Counter[str] = Counter()
    secondary_counts: Counter[str] = Counter()
    pending_count = 0

    for row in rows:
        segment_id = int(row["segment_id"])
        state = states.get(segment_id)
        if state and state["state_group"] == "pending" and int(state["is_closed"] or 0) == 0:
            pending_count += 1
        blob = " ".join(str(row.get(key) or "") for key in ("relative_path", "source_key", "old_text", "confirmed_text", "output_text"))
        script_markers = markers(SCRIPT_VALUE_MARKERS, blob)
        numeric_markers = markers(NUMERIC_MARKERS, blob)
        requirement_markers = markers(REQUIREMENT_MARKERS, blob)
        secondary_markers = markers(SECONDARY_MARKERS, blob)
        decision, next_component, rationale = classify(state, script_markers, numeric_markers, secondary_markers)
        if decision not in ALLOWED_DECISIONS:
            raise SystemExit(f"unknown decision {decision} for segment_id {segment_id}")
        decision_counts[decision] += 1
        script_counts.update(script_markers or ["NoScriptValueMarker"])
        numeric_counts.update(numeric_markers or ["NoNumericMarker"])
        requirement_counts.update(requirement_markers or ["NoRequirementMarker"])
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
            "script_value_markers": script_markers,
            "numeric_markers": numeric_markers,
            "requirement_markers": requirement_markers,
            "secondary_markers": secondary_markers,
            "script_value_decision": decision,
            "next_component": next_component,
            "requires_lifecycle_later": False,
            "requires_apply_later": False,
            "corrected_text": "",
            "rationale": rationale,
        })

    terminal_count = sum(count for decision, count in decision_counts.items() if decision.startswith("script_value_terminal_policy"))
    dominant, dominant_count = decision_counts.most_common(1)[0] if decision_counts else ("none", 0)
    if dominant.startswith("needs_") and dominant_count >= 10:
        next_prompt = f"chat_exec_{dominant.removeprefix('needs_')}_review_prompt.md"
        stop_rule = f"continue_narrow_prompt: {dominant} reached {dominant_count} >= 10"
    elif terminal_count > len(results) / 2:
        next_prompt = "chat_exec_concept_requirement_policy_review_prompt.md"
        stop_rule = "terminal_majority_return_to_concept_requirement"
    else:
        next_prompt = "chat_exec_concept_requirement_policy_review_prompt.md"
        stop_rule = "fragmented_return_to_concept_requirement"

    spec = {
        "schema_version": 1,
        "created_for": "read_only_subpolicy_design",
        "parent_policy": "requirement_tooltip_policy",
        "policy_id": "script_value_requirement_policy",
        "segment_state_run_id": args.segment_state_run_id,
        "ledger_run_id": LEDGER_RUN_ID,
        "entry_conditions": [
            "requirement_tooltip_decision == needs_script_value_requirement_policy",
            "segment remains pending",
            "needs_output_apply == 0",
            "confirmed_matches_output == 1",
        ],
        "script_value_types": [{"decision": decision, "sampled": count} for decision, count in decision_counts.most_common()],
        "resolution_order": [
            "state guard",
            "residual and effect-list guards",
            "ScriptValue with numeric/currency formatting",
            "percent/modifier",
            "title/law and trait/accolade escapes",
            "scope/getter terminal guard",
            "domain/event/dynamic parser fallback",
        ],
        "next_components": [
            "script_value_numeric_modifier_policy",
            "script_value_percent_modifier_policy",
            "script_value_title_law_policy",
            "script_value_trait_accolade_policy",
            "script_value_scope_getter_policy",
            "script_value_concept_policy",
            "script_value_effect_list_policy",
            "concept_requirement_policy",
        ],
        "blocked_conditions": ["state guard failed", "visible residual", "ambiguous dynamic value surface"],
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
            "script_value_marker_counts": dict(script_counts),
            "numeric_marker_counts": dict(numeric_counts),
            "requirement_marker_counts": dict(requirement_counts),
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
            handle.write(json.dumps({"record_type": "decision_count", "script_value_decision": decision, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for marker, count in script_counts.most_common():
            handle.write(json.dumps({"record_type": "script_value_marker_count", "marker": marker, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for marker, count in numeric_counts.most_common():
            handle.write(json.dumps({"record_type": "numeric_marker_count", "marker": marker, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for marker, count in requirement_counts.most_common():
            handle.write(json.dumps({"record_type": "requirement_marker_count", "marker": marker, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for marker, count in secondary_counts.most_common():
            handle.write(json.dumps({"record_type": "secondary_marker_count", "marker": marker, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for priority, (prompt, rationale) in enumerate([
            (next_prompt, "selected by ScriptValue stop rule"),
            ("chat_exec_concept_requirement_policy_review_prompt.md", "next larger requirement tooltip block"),
            ("chat_exec_requirement_effect_router_component_integration_review_prompt.md", "architecture/integration option after terminal policy consolidation"),
        ], 1):
            handle.write(json.dumps({"record_type": "strategy", "priority": priority, "next_prompt": prompt, "rationale": rationale}, ensure_ascii=False, sort_keys=True) + "\n")
        for result in results:
            handle.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")

    with spec_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(spec, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("ScriptValue requirement policy review\n\n")
        handle.write(f"total_revisado: {len(results)}\n")
        handle.write(f"pending_count: {pending_count}\n")
        handle.write("ready_lifecycle_future: 0\n")
        handle.write("apply_candidates_future: 0\n")
        handle.write(f"subtipo_dominante: {dominant}\n")
        handle.write(f"dominant_count: {dominant_count}\n")
        handle.write(f"regra_de_parada: {stop_rule}\n")
        handle.write(f"proximo_prompt: {next_prompt}\n\n")
        handle.write("script_value_decision_counts:\n")
        for decision, count in decision_counts.most_common():
            handle.write(f"- {decision}: {count}\n")
        handle.write("\nTop ScriptValue markers:\n")
        for marker, count in script_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nTop numeric markers:\n")
        for marker, count in numeric_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nTop requirement markers:\n")
        for marker, count in requirement_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nTop secondary markers:\n")
        for marker, count in secondary_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nAnalise\n")
        handle.write("- script_value_requirement_policy deve virar componente read-only real.\n")
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
