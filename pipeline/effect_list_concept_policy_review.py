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


SOURCE_DECISION = "needs_effect_list_concept_policy"
PRIMARY_ROUTE = "effect_list_multiline_policy"
PARENT_POLICY = "effect_list_multiline_policy"
LEDGER_RUN_ID = 76

REGISTERED_POLICY_KEYS = {
    "concept_requirement_policy",
    "effect_list_script_value_policy",
    "effect_list_trait_accolade_policy",
    "effect_list_gender_local_player_policy",
}

ALLOWED_DECISIONS = {
    "effect_concept_terminal_policy",
    "effect_concept_terminal_policy_with_effect_list_guard",
    "effect_concept_terminal_policy_with_domain_guard",
    "effect_concept_terminal_policy_with_scope_guard",
    "effect_concept_terminal_policy_with_event_guard",
    "effect_concept_reuse_concept_requirement_policy",
    "effect_concept_reuse_effect_list_script_value_policy",
    "effect_concept_reuse_effect_list_trait_accolade_policy",
    "effect_concept_reuse_effect_list_gender_local_player_policy",
    "needs_effect_concept_artifact_activity_policy",
    "needs_effect_concept_trait_accolade_policy",
    "needs_effect_concept_script_value_policy",
    "needs_effect_concept_title_law_policy",
    "needs_effect_concept_scope_getter_policy",
    "needs_effect_concept_gender_local_player_policy",
    "needs_effect_concept_event_context",
    "needs_effect_concept_domain_context",
    "needs_effect_concept_residual_repair",
    "needs_effect_concept_dynamic_parser_escape",
    "effect_concept_blocked_uncertain",
}

EFFECT_LIST_MARKERS = [
    ("Multiline", re.compile(r"\n|Multiline|EFFECT_LIST_BULLET|BULLET_WITH_TAB|\\n", re.I)),
    ("Tooltip", re.compile(r"tooltip|_tt\b|\.tt\b|#T|#help", re.I)),
    ("EffectListBullet", re.compile(r"EFFECT_LIST_BULLET|BULLET_WITH_TAB|\$TAB|\$BULLET", re.I)),
]

CONCEPT_MARKERS = [
    ("ConceptFunction", re.compile(r"Concept\(", re.I)),
    ("GameConcept", re.compile(r"game_concept|\\$game_concept_", re.I)),
    ("ConceptLink", re.compile(r"\[[A-Za-z0-9_]+\|l?E\]", re.I)),
    ("ConceptKey", re.compile(r"\[[A-Za-z0-9_]+\|", re.I)),
]

GUARD_MARKERS = [
    ("EffectListGuard", re.compile(r"\n|Multiline|EFFECT_LIST_BULLET|BULLET_WITH_TAB|tooltip|_tt\b|\.tt\b|#T|#help", re.I)),
    ("DomainGuard", re.compile(r"Domain|faith|religion|realm|government|province|culture|dynasty|ledger|law|scheme|combat|council", re.I)),
    ("ScopeGuard", re.compile(r"ROOT\.|FROM\.|SCOPE\.|Get[A-Za-z0-9_]+\(|recipient\.|Combat\.|SituationHistoryEntry", re.I)),
    ("EventGuard", re.compile(r"event|events|\.desc|desc_|interaction|decision|situation", re.I)),
    ("ScriptValueGuard", re.compile(r"ScriptValue|GetScriptValue|GetValue|\|V[0-9]?|#V|[0-9]+\s*%", re.I)),
    ("TraitAccoladeGuard", re.compile(r"TraitAccolade|AccoladeTrait|GetTrait|trait|accolade|knight|virtue|sin", re.I)),
    ("GenderLocalPlayerGuard", re.compile(r"GenderLocalPlayer|Select_CString|ES_(?:OA|XA|EA|ElLa|DelDela|AlAla|A|O)|GetPlayer|\b[Vv]ocê\b|\bseu\b|\bsua\b", re.I)),
    ("ArtifactActivityGuard", re.compile(r"ArtifactActivity|activity|combat|scheme|decision", re.I)),
    ("TitleLawGuard", re.compile(r"TitleLaw|GetLaw|law|title|county|domain|liege", re.I)),
]

SECONDARY_MARKERS = [
    ("Concept", re.compile(r"Concept|game_concept|\\$game_concept_|\[[A-Za-z0-9_]+\|", re.I)),
    ("ScriptValue", re.compile(r"ScriptValue|GetScriptValue|GetValue|\|V[0-9]?|#V|[0-9]+\s*%", re.I)),
    ("TraitAccolade", re.compile(r"TraitAccolade|AccoladeTrait|GetTrait|trait|accolade|knight|virtue|sin", re.I)),
    ("GenderLocalPlayer", re.compile(r"GenderLocalPlayer|Select_CString|ES_(?:OA|XA|EA|ElLa|DelDela|AlAla|A|O)|GetPlayer|\b[Vv]ocê\b|\bseu\b|\bsua\b", re.I)),
    ("ArtifactActivity", re.compile(r"ArtifactActivity|activity|combat|scheme|decision", re.I)),
    ("TitleLaw", re.compile(r"TitleLaw|GetLaw|law|title|county|domain|liege", re.I)),
    ("ScopeGetter", re.compile(r"ROOT\.|FROM\.|SCOPE\.|Get[A-Za-z0-9_]+\(|recipient\.|Combat\.|SituationHistoryEntry", re.I)),
    ("Event", re.compile(r"event|events|\.desc|desc_|interaction|decision|situation", re.I)),
    ("Domain", re.compile(r"Domain|faith|religion|realm|government|province|culture|dynasty|ledger|law|scheme|combat|council", re.I)),
    ("ResidualVisible", re.compile(r"ÃƒÆ’|Ãƒâ€š|Ã‚Â¿|Ã‚Â¡|Ã¢â‚¬â„¢|Ã¢â‚¬Å“|Ã¢â‚¬ï¿½|ï¿½|\bthe\b|\byour\b|\byou\b|\btheir\b|\bcannot\b", re.I)),
    ("DynamicToken", re.compile(r"Custom\(|Select_CString|Concept\(|ScriptValue|GetTrait|GetScheme|GetSituationType|ROOT\.|FROM\.|SCOPE\.|TARGET\.|\[[^\]]+\]|\$[^$]+\$|dynamic_ck3_expression|dynamictoken", re.I)),
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
        and row.get("effect_list_multiline_decision") == SOURCE_DECISION
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
        SELECT segment_id, state_group, is_closed, needs_output_apply, confirmed_matches_output
        FROM segment_state_items
        WHERE run_id = ?
          AND segment_id IN ({placeholders})
        """,
        (run_id, *segment_ids),
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def fetch_texts(conn: sqlite3.Connection, segment_ids: list[int]) -> dict[int, dict[str, str]]:
    if not segment_ids:
        return {}
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT
          ss.id AS segment_id,
          ss.old_text,
          ss.spanish_text,
          os.portuguese_text AS output_text,
          (
            SELECT sc.confirmed_text
            FROM segment_confirmations sc
            WHERE sc.segment_id = ss.id
            ORDER BY sc.updated_at DESC, sc.id DESC
            LIMIT 1
          ) AS confirmed_text
        FROM source_segments ss
        LEFT JOIN output_segments os ON os.segment_id = ss.id
        WHERE ss.id IN ({placeholders})
        """,
        tuple(segment_ids),
    ).fetchall()
    return {
        int(row["segment_id"]): {
            "old_text": row["old_text"] or row["spanish_text"] or "",
            "confirmed_text": row["confirmed_text"] or "",
            "output_text": row["output_text"] or "",
        }
        for row in rows
    }


def registered_policies(conn: sqlite3.Connection) -> set[str]:
    placeholders = ",".join("?" for _ in REGISTERED_POLICY_KEYS)
    rows = conn.execute(
        f"""
        SELECT agent_key
        FROM ml_agent_registry
        WHERE agent_key IN ({placeholders})
          AND status = 'active'
        """,
        tuple(sorted(REGISTERED_POLICY_KEYS)),
    ).fetchall()
    return {str(row["agent_key"]) for row in rows}


def detect(patterns: list[tuple[str, re.Pattern[str]]], blob: str) -> list[str]:
    return [name for name, pattern in patterns if pattern.search(blob)]


def classify(
    state: dict[str, Any] | None,
    registered: set[str],
    concept_markers: list[str],
    guard_markers: list[str],
    secondary_markers: list[str],
) -> tuple[str, str, str, str]:
    guards = set(guard_markers)
    secondary = set(secondary_markers)
    concepts = set(concept_markers)
    if not state or state["state_group"] != "pending" or int(state["is_closed"] or 0) != 0:
        return "effect_concept_blocked_uncertain", "", "state_guard", "segment is not pending in selected state run"
    if int(state["needs_output_apply"] or 0) != 0 or int(state["confirmed_matches_output"] or 0) != 1:
        return "effect_concept_blocked_uncertain", "", "state_guard", "state guard failed: needs_output_apply or confirmed_matches_output"
    if "ResidualVisible" in secondary:
        return "needs_effect_concept_residual_repair", "", "residual_dependency_filtered_repair", "visible residual remains"

    if ("ScriptValue" in secondary or "ScriptValueGuard" in guards) and "effect_list_script_value_policy" in registered:
        return "effect_concept_reuse_effect_list_script_value_policy", "effect_list_script_value_policy", "effect_list_script_value_policy", "can reuse registered effect-list ScriptValue policy"
    if ("TraitAccolade" in secondary or "TraitAccoladeGuard" in guards) and "effect_list_trait_accolade_policy" in registered:
        return "effect_concept_reuse_effect_list_trait_accolade_policy", "effect_list_trait_accolade_policy", "effect_list_trait_accolade_policy", "can reuse registered effect-list trait/accolade policy"
    if ("GenderLocalPlayer" in secondary or "GenderLocalPlayerGuard" in guards) and "effect_list_gender_local_player_policy" in registered:
        return "effect_concept_reuse_effect_list_gender_local_player_policy", "effect_list_gender_local_player_policy", "effect_list_gender_local_player_policy", "can reuse registered effect-list gender/local-player policy"
    if concepts and "concept_requirement_policy" in registered:
        return "effect_concept_reuse_concept_requirement_policy", "concept_requirement_policy", "concept_requirement_policy", "can reuse registered concept requirement policy with effect-list guard"

    if "ArtifactActivity" in secondary or "ArtifactActivityGuard" in guards:
        return "needs_effect_concept_artifact_activity_policy", "", "effect_concept_artifact_activity_policy", "artifact/activity dependency dominates"
    if "TitleLaw" in secondary or "TitleLawGuard" in guards:
        return "needs_effect_concept_title_law_policy", "", "effect_concept_title_law_policy", "title/law dependency dominates"
    if "ScopeGetter" in secondary or "ScopeGuard" in guards:
        return "needs_effect_concept_scope_getter_policy", "", "effect_concept_scope_getter_policy", "scope/getter dependency dominates"
    if "Event" in secondary or "EventGuard" in guards:
        return "effect_concept_terminal_policy_with_event_guard", "", "effect_list_concept_policy", "terminal concept surface with event guard"
    if "Domain" in secondary or "DomainGuard" in guards:
        return "effect_concept_terminal_policy_with_domain_guard", "", "effect_list_concept_policy", "terminal concept surface with domain guard"
    if "DynamicToken" in secondary:
        return "needs_effect_concept_dynamic_parser_escape", "", "ck3_dynamic_symbolic_parser", "dynamic parser escape remains"
    if "EffectListGuard" in guards:
        return "effect_concept_terminal_policy_with_effect_list_guard", "", "effect_list_concept_policy", "terminal concept effect-list surface"
    return "effect_concept_blocked_uncertain", "", "human_review_or_evidence_collection", "insufficient concept evidence"


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_effect_list_concept_policy_review"
    spec = reports_dir / f"{stamp}_effect_list_concept_policy_spec.json"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), spec


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only effect-list concept policy review.")
    parser.add_argument("--effect-list-jsonl", required=True, type=Path)
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    args = parser.parse_args()

    rows = source_rows(args.effect_list_jsonl)
    segment_ids = [int(row["segment_id"]) for row in rows]
    conn = connect_readonly()
    states = fetch_states(conn, args.segment_state_run_id, segment_ids)
    texts = fetch_texts(conn, segment_ids)
    registered = registered_policies(conn)

    results: list[dict[str, Any]] = []
    decision_counts: Counter[str] = Counter()
    policy_counts: Counter[str] = Counter()
    family_counts: Counter[str] = Counter()
    effect_counts: Counter[str] = Counter()
    concept_counts: Counter[str] = Counter()
    guard_counts: Counter[str] = Counter()
    secondary_counts: Counter[str] = Counter()
    pending_count = 0

    for row in rows:
        segment_id = int(row["segment_id"])
        state = states.get(segment_id)
        text = texts.get(segment_id, {})
        old_text = str(row.get("old_text") or text.get("old_text") or "")
        confirmed_text = str(row.get("confirmed_text") or text.get("confirmed_text") or "")
        output_text = str(row.get("output_text") or text.get("output_text") or "")
        if state and state["state_group"] == "pending" and int(state["is_closed"] or 0) == 0:
            pending_count += 1
        blob = " ".join(
            [
                str(row.get("relative_path") or ""),
                str(row.get("source_key") or ""),
                old_text,
                confirmed_text,
                output_text,
                " ".join(row.get("families_open") or []),
                " ".join(row.get("secondary_markers") or []),
            ]
        )
        effect_markers = detect(EFFECT_LIST_MARKERS, blob)
        concept_markers = detect(CONCEPT_MARKERS, blob)
        guard_markers = detect(GUARD_MARKERS, blob)
        secondary_markers = sorted(set(row.get("secondary_markers") or []) | set(detect(SECONDARY_MARKERS, blob)))
        decision, matched_policy, next_component, rationale = classify(
            state,
            registered,
            concept_markers,
            guard_markers,
            secondary_markers,
        )
        if decision not in ALLOWED_DECISIONS:
            raise SystemExit(f"unknown decision {decision} for segment_id {segment_id}")
        decision_counts[decision] += 1
        if matched_policy:
            policy_counts[matched_policy] += 1
        family_counts.update(row.get("families_open") or ["NoOpenFamily"])
        effect_counts.update(effect_markers or ["NoEffectListMarker"])
        concept_counts.update(concept_markers or ["NoConceptMarker"])
        guard_counts.update(guard_markers or ["NoGuardMarker"])
        secondary_counts.update(secondary_markers or ["NoSecondaryMarker"])
        results.append(
            {
                "record_type": "sample_review",
                "segment_id": segment_id,
                "relative_path": str(row.get("relative_path") or ""),
                "source_key": str(row.get("source_key") or ""),
                "families_open": list(row.get("families_open") or []),
                "source_decision": SOURCE_DECISION,
                "primary_route": PRIMARY_ROUTE,
                "old_text": old_text,
                "confirmed_text": confirmed_text,
                "output_text": output_text,
                "effect_list_markers": effect_markers,
                "concept_markers": concept_markers,
                "matched_registered_policy": matched_policy,
                "guard_markers": guard_markers,
                "secondary_markers": secondary_markers,
                "effect_concept_decision": decision,
                "next_component": next_component,
                "requires_lifecycle_later": False,
                "requires_apply_later": False,
                "corrected_text": "",
                "rationale": rationale,
            }
        )

    reuse_count = sum(count for decision, count in decision_counts.items() if decision.startswith("effect_concept_reuse_"))
    terminal_count = sum(count for decision, count in decision_counts.items() if decision.startswith("effect_concept_terminal_policy"))
    dominant, dominant_count = decision_counts.most_common(1)[0] if decision_counts else ("none", 0)
    if reuse_count > len(results) / 2:
        next_prompt = "chat_exec_effect_list_concept_terminal_spec_registration_prompt.md"
        stop_rule = f"reuse_majority_terminal_readonly: {reuse_count}/{len(results)} reuse cataloged policies"
        policy_shape = "terminal_reuse_route"
    elif terminal_count > len(results) / 2:
        next_prompt = "chat_exec_effect_list_concept_terminal_spec_registration_prompt.md"
        stop_rule = f"terminal_majority_readonly: {terminal_count}/{len(results)} terminal policies"
        policy_shape = "terminal_policy"
    elif dominant.startswith("needs_") and dominant_count >= 8:
        next_prompt = f"chat_exec_{dominant.removeprefix('needs_')}_review_prompt.md"
        stop_rule = f"continue_narrow_prompt: {dominant} reached {dominant_count} >= 8"
        policy_shape = "splitter"
    else:
        next_prompt = "chat_exec_effect_list_policy_catalog_integration_prompt.md"
        stop_rule = "fragmented_recommend_effect_list_partial_integration"
        policy_shape = "fragmented_splitter_guard"

    spec = {
        "schema_version": 1,
        "created_for": "read_only_subpolicy_design",
        "parent_policy": PARENT_POLICY,
        "policy_id": "effect_list_concept_policy",
        "segment_state_run_id": args.segment_state_run_id,
        "ledger_run_id": LEDGER_RUN_ID,
        "entry_conditions": [
            "effect_list_multiline_decision == needs_effect_list_concept_policy",
            "primary_route == effect_list_multiline_policy",
            "segment remains pending",
            "needs_output_apply == 0",
            "confirmed_matches_output == 1",
        ],
        "reused_registered_policies": [{"agent_key": key, "sampled": policy_counts.get(key, 0)} for key in sorted(registered & REGISTERED_POLICY_KEYS)],
        "effect_concept_types": [{"decision": decision, "sampled": count} for decision, count in decision_counts.most_common()],
        "resolution_order": [
            "state guard",
            "residual guard",
            "registered effect-list ScriptValue/trait/gender routes",
            "concept_requirement_policy",
            "artifact/activity",
            "title/law and scope/getter",
            "event/domain",
        ],
        "next_components": [
            "concept_requirement_policy",
            "effect_list_script_value_policy",
            "effect_list_trait_accolade_policy",
            "effect_list_gender_local_player_policy",
            "effect_list_policy_catalog_integration",
        ],
        "blocked_conditions": ["state guard failed", "visible residual", "missing concept marker"],
        "promotion_gate": "read_only_component_only_no_apply_no_lifecycle",
        "sampled": len(results),
        "reuse_cataloged_policy_count": reuse_count,
        "terminal_policy_count": terminal_count,
        "policy_shape": policy_shape,
        "stop_rule": stop_rule,
    }

    txt_path, jsonl_path, spec_path = output_paths()
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps({
            "record_type": "summary",
            "source_decision": SOURCE_DECISION,
            "primary_route": PRIMARY_ROUTE,
            "parent_policy": PARENT_POLICY,
            "registered_policies_found": sorted(registered & REGISTERED_POLICY_KEYS),
            "total_reviewed": len(results),
            "pending_count": pending_count,
            "decision_counts": dict(decision_counts),
            "matched_policy_counts": dict(policy_counts),
            "family_counts": dict(family_counts),
            "effect_list_marker_counts": dict(effect_counts),
            "concept_marker_counts": dict(concept_counts),
            "guard_marker_counts": dict(guard_counts),
            "secondary_marker_counts": dict(secondary_counts),
            "reuse_cataloged_policy_count": reuse_count,
            "terminal_policy_count": terminal_count,
            "ready_lifecycle_future": 0,
            "apply_candidates_future": 0,
            "dominant_subtype": dominant,
            "dominant_count": dominant_count,
            "policy_shape": policy_shape,
            "stop_rule": stop_rule,
            "next_prompt": next_prompt,
            "requires_lifecycle_later": False,
            "requires_apply_later": False,
        }, ensure_ascii=False, sort_keys=True) + "\n")
        for decision, count in decision_counts.most_common():
            handle.write(json.dumps({"record_type": "decision_count", "effect_concept_decision": decision, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for policy, count in policy_counts.most_common():
            handle.write(json.dumps({"record_type": "matched_policy_count", "matched_registered_policy": policy, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for counter_name, counter in [
            ("family_count", family_counts),
            ("effect_list_marker_count", effect_counts),
            ("concept_marker_count", concept_counts),
            ("guard_marker_count", guard_counts),
            ("secondary_marker_count", secondary_counts),
        ]:
            for marker, count in counter.most_common():
                handle.write(json.dumps({"record_type": counter_name, "marker": marker, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for priority, (prompt, rationale) in enumerate([
            (next_prompt, "selected by effect-list concept stop rule"),
            ("chat_exec_effect_list_policy_catalog_integration_prompt.md", "integrate partial effect-list package"),
            ("chat_exec_global_post_architecture_diagnostic_prompt.md", "global check after effect-list expansion"),
        ], 1):
            handle.write(json.dumps({"record_type": "strategy", "priority": priority, "next_prompt": prompt, "rationale": rationale}, ensure_ascii=False, sort_keys=True) + "\n")
        for result in results:
            handle.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")

    with spec_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(spec, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("Effect-list concept policy review\n\n")
        handle.write(f"- total revisado: {len(results)}\n")
        handle.write(f"- reuso de policies/specs catalogadas: {reuse_count}\n")
        handle.write("- ready_lifecycle_future: 0\n")
        handle.write("- apply_candidates_future: 0\n")
        handle.write(f"- subtipo dominante: {dominant}\n")
        handle.write(f"- dominant_count: {dominant_count}\n")
        handle.write(f"- policy_shape: {policy_shape}\n")
        handle.write(f"- regra_de_parada: {stop_rule}\n")
        handle.write(f"- proximo_prompt: {next_prompt}\n\n")
        handle.write("effect_concept_decision_counts:\n")
        for decision, count in decision_counts.most_common():
            handle.write(f"- {decision}: {count}\n")
        handle.write("\nMatched policies/specs:\n")
        for policy, count in policy_counts.most_common():
            handle.write(f"- {policy}: {count}\n")
        handle.write("\nTop families_open:\n")
        for family, count in family_counts.most_common(15):
            handle.write(f"- {family}: {count}\n")
        handle.write("\nTop effect-list markers:\n")
        for marker, count in effect_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nTop concept markers:\n")
        for marker, count in concept_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nTop guard markers:\n")
        for marker, count in guard_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nTop secondary markers:\n")
        for marker, count in secondary_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nAnalise\n")
        handle.write("- effect_list_concept_policy deve virar componente read-only se houver maioria de reuso ou terminalidade.\n")
        handle.write("- Esta revisao nao gera lifecycle/apply.\n")
        handle.write("- Se terminalizar, registrar depois; se fragmentar, partir para integracao parcial do pacote effect-list.\n")

    print(f"txt: {txt_path}")
    print(f"jsonl: {jsonl_path}")
    print(f"spec: {spec_path}")
    print(f"total_reviewed: {len(results)}")
    print(f"reuse_cataloged_policy_count: {reuse_count}")
    print("ready_lifecycle_future: 0")
    print("apply_candidates_future: 0")
    print(f"stop_rule: {stop_rule}")
    print("decision_counts:")
    for decision, count in decision_counts.most_common():
        print(f"  {decision}: {count}")


if __name__ == "__main__":
    main()
