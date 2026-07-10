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


SOURCE_DECISION = "needs_effect_list_trait_accolade_policy"
PRIMARY_ROUTE = "effect_list_multiline_policy"
PARENT_POLICY = "effect_list_multiline_policy"
LEDGER_RUN_ID = 76

REGISTERED_POLICY_KEYS = {
    "get_trait_accolade_requirement_policy",
    "get_trait_scope_requirement_policy",
    "accolade_knight_attribute_policy",
    "knight_attribute_unlock_requirement_policy",
    "unlock_acclaimed_knight_entity_policy",
    "acclaimed_knight_entity_requirement_policy",
    "acclaimed_knight_entity_unlock_final_policy",
}

ALLOWED_DECISIONS = {
    "effect_trait_reuse_get_trait_accolade_policy",
    "effect_trait_reuse_accolade_knight_attribute_policy",
    "effect_trait_reuse_knight_attribute_unlock_policy",
    "effect_trait_reuse_acclaimed_knight_entity_policy",
    "effect_trait_terminal_policy_with_effect_list_guard",
    "effect_trait_terminal_policy_with_event_guard",
    "effect_trait_terminal_policy_with_domain_guard",
    "needs_effect_trait_get_trait_scope_policy",
    "needs_effect_trait_knight_attribute_policy",
    "needs_effect_trait_acclaimed_knight_policy",
    "needs_effect_trait_artifact_activity_policy",
    "needs_effect_trait_gender_local_player_policy",
    "needs_effect_trait_script_value_policy",
    "needs_effect_trait_concept_policy",
    "needs_effect_trait_title_law_policy",
    "needs_effect_trait_event_context",
    "needs_effect_trait_domain_context",
    "needs_effect_trait_residual_repair",
    "needs_effect_trait_dynamic_parser_escape",
    "effect_trait_blocked_uncertain",
}

EFFECT_LIST_MARKERS = [
    ("Multiline", re.compile(r"\n|Multiline|EFFECT_LIST_BULLET|BULLET_WITH_TAB|\\n", re.I)),
    ("Tooltip", re.compile(r"tooltip|_tt\b|\.tt\b|#T|#help", re.I)),
    ("EffectListBullet", re.compile(r"EFFECT_LIST_BULLET|BULLET_WITH_TAB|\$BULLET", re.I)),
]

TRAIT_MARKERS = [
    ("GetTrait", re.compile(r"GetTrait\(", re.I)),
    ("TraitConcept", re.compile(r"\[trait\|l?E\]|\btrait\b|trait_track|TraitAccolade", re.I)),
    ("Modifier", re.compile(r"modifier|BuildingModifier|building|domicile_building|estate_building", re.I)),
]

ACCOLADE_MARKERS = [
    ("Accolade", re.compile(r"accolade|acclaimed_knight|Accolade|glory", re.I)),
    ("AcclaimedKnight", re.compile(r"acclaimed_knight|acclaimed knight|acclaimed", re.I)),
]

KNIGHT_MARKERS = [
    ("Knight", re.compile(r"knight|knights|KnightCulture|acclaimed_knight", re.I)),
    ("KnightAttribute", re.compile(r"attribute|unlock|rank|glory|AccoladeRank", re.I)),
]

GUARD_MARKERS = [
    ("EffectListGuard", re.compile(r"\n|Multiline|EFFECT_LIST_BULLET|BULLET_WITH_TAB|tooltip|_tt\b|\.tt\b|#T|#help", re.I)),
    ("ArtifactActivityGuard", re.compile(r"ArtifactActivity|activity|tournament|travel|tour|pilgrimage|survey", re.I)),
    ("GenderLocalPlayerGuard", re.compile(r"GenderLocalPlayer|Select_CString|ES_(?:OA|XA|EA|ElLa|DelDela|AlAla|A|O)|GetPlayer|\b[Vv]ocê\b|\bseu\b|\bsua\b", re.I)),
    ("ScriptValueGuard", re.compile(r"ScriptValue|GetScriptValue|EmptyScope|GetValue|\|V[0-9]?|#V|[0-9]+\s*%", re.I)),
    ("ConceptGuard", re.compile(r"Concept|game_concept|\[[A-Za-z0-9_]+\|", re.I)),
    ("TitleLawGuard", re.compile(r"TitleLaw|GetLaw|law|succession|title|county|domain", re.I)),
    ("EventGuard", re.compile(r"event|events|\.desc|desc_|modifier_desc|notification|tooltip", re.I)),
    ("DomainGuard", re.compile(r"Domain|building|modifier|game_concept|faith|religion|realm|government|province|domicile|county", re.I)),
]

SECONDARY_MARKERS = [
    ("AccoladeTrait", re.compile(r"AccoladeTrait|accolade|acclaimed_knight|glory|Accolade", re.I)),
    ("TraitAccolade", re.compile(r"TraitAccolade|GetTrait|trait_track|\[trait\|l?E\]|\btrait\b", re.I)),
    ("KnightAttribute", re.compile(r"knight|KnightCulture|attribute|unlock|AccoladeRank", re.I)),
    ("BuildingModifier", re.compile(r"BuildingModifier|modifier|building|domicile_building|estate_building", re.I)),
    ("ArtifactActivity", re.compile(r"ArtifactActivity|activity|tournament|travel|tour|pilgrimage|survey", re.I)),
    ("GenderLocalPlayer", re.compile(r"GenderLocalPlayer|Select_CString|ES_(?:OA|XA|EA|ElLa|DelDela|AlAla|A|O)|GetPlayer|\b[Vv]ocê\b|\bseu\b|\bsua\b", re.I)),
    ("ScriptValue", re.compile(r"ScriptValue|GetScriptValue|EmptyScope|GetValue|\|V[0-9]?|#V|[0-9]+\s*%", re.I)),
    ("Concept", re.compile(r"Concept|game_concept|\[[A-Za-z0-9_]+\|", re.I)),
    ("TitleLaw", re.compile(r"TitleLaw|GetLaw|law|succession|title|county|domain", re.I)),
    ("Event", re.compile(r"event|events|\.desc|desc_|notification", re.I)),
    ("Domain", re.compile(r"Domain|building|modifier|game_concept|faith|religion|realm|government|province|domicile|county", re.I)),
    ("ScopeGetter", re.compile(r"ROOT\.|FROM\.|SCOPE\.|Get[A-Za-z0-9_]+\(|Accolade\.|liege\.|recipient\.", re.I)),
    ("ResidualVisible", re.compile(r"ÃƒÆ’|Ãƒâ€š|Ã‚Â¿|Ã‚Â¡|Ã¢â‚¬â„¢|Ã¢â‚¬Å“|Ã¢â‚¬ï¿½|ï¿½|\bthe\b|\byour\b|\byou\b|\btheir\b|\bcannot\b", re.I)),
    ("DynamicToken", re.compile(r"Custom\(|Select_CString|Concept\(|ScriptValue|GetTrait|GetLaw|ROOT\.|FROM\.|SCOPE\.|TARGET\.|\[[^\]]+\]|\$[^$]+\$|dynamic_ck3_expression|dynamictoken", re.I)),
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


def choose_reuse_policy(
    registered: set[str],
    trait_markers: list[str],
    accolade_markers: list[str],
    knight_markers: list[str],
    secondary_markers: list[str],
) -> tuple[str, str] | None:
    trait = set(trait_markers)
    accolade = set(accolade_markers)
    knight = set(knight_markers)
    secondary = set(secondary_markers)
    if "AcclaimedKnight" in accolade and "acclaimed_knight_entity_unlock_final_policy" in registered:
        return "effect_trait_reuse_acclaimed_knight_entity_policy", "acclaimed_knight_entity_unlock_final_policy"
    if "Accolade" in accolade and "Knight" in knight and "accolade_knight_attribute_policy" in registered:
        return "effect_trait_reuse_accolade_knight_attribute_policy", "accolade_knight_attribute_policy"
    if "KnightAttribute" in secondary and "knight_attribute_unlock_requirement_policy" in registered:
        return "effect_trait_reuse_knight_attribute_unlock_policy", "knight_attribute_unlock_requirement_policy"
    if ("GetTrait" in trait or "TraitConcept" in trait) and "get_trait_accolade_requirement_policy" in registered:
        return "effect_trait_reuse_get_trait_accolade_policy", "get_trait_accolade_requirement_policy"
    return None


def classify(
    state: dict[str, Any] | None,
    registered: set[str],
    trait_markers: list[str],
    accolade_markers: list[str],
    knight_markers: list[str],
    guard_markers: list[str],
    secondary_markers: list[str],
) -> tuple[str, str, str, str]:
    guards = set(guard_markers)
    secondary = set(secondary_markers)
    if not state or state["state_group"] != "pending" or int(state["is_closed"] or 0) != 0:
        return "effect_trait_blocked_uncertain", "", "state_guard", "segment is not pending in selected state run"
    if int(state["needs_output_apply"] or 0) != 0 or int(state["confirmed_matches_output"] or 0) != 1:
        return "effect_trait_blocked_uncertain", "", "state_guard", "state guard failed: needs_output_apply or confirmed_matches_output"
    if "ResidualVisible" in secondary:
        return "needs_effect_trait_residual_repair", "", "residual_dependency_filtered_repair", "visible residual remains"

    reuse = choose_reuse_policy(registered, trait_markers, accolade_markers, knight_markers, secondary_markers)
    if reuse:
        decision, policy = reuse
        return decision, policy, policy, f"can reuse cataloged policy {policy} with effect-list guard"

    if "ScriptValue" in secondary or "ScriptValueGuard" in guards:
        return "needs_effect_trait_script_value_policy", "", "effect_trait_script_value_policy", "ScriptValue dependency dominates"
    if "GenderLocalPlayer" in secondary or "GenderLocalPlayerGuard" in guards:
        return "needs_effect_trait_gender_local_player_policy", "", "effect_trait_gender_local_player_policy", "gender/local-player dependency dominates"
    if "ArtifactActivity" in secondary or "ArtifactActivityGuard" in guards:
        return "needs_effect_trait_artifact_activity_policy", "", "effect_trait_artifact_activity_policy", "artifact/activity dependency dominates"
    if "AccoladeTrait" in secondary or "AcclaimedKnight" in accolade_markers:
        return "needs_effect_trait_acclaimed_knight_policy", "", "effect_trait_acclaimed_knight_policy", "accolade/acclaimed knight evidence lacks reusable terminal route"
    if "KnightAttribute" in secondary:
        return "needs_effect_trait_knight_attribute_policy", "", "effect_trait_knight_attribute_policy", "knight attribute evidence dominates"
    if "ScopeGetter" in secondary:
        return "needs_effect_trait_get_trait_scope_policy", "", "effect_trait_get_trait_scope_policy", "scope/getter trait evidence dominates"
    if "Concept" in secondary or "ConceptGuard" in guards:
        return "needs_effect_trait_concept_policy", "", "effect_trait_concept_policy", "concept/domain trait surface dominates"
    if "TitleLaw" in secondary or "TitleLawGuard" in guards:
        return "needs_effect_trait_title_law_policy", "", "effect_trait_title_law_policy", "title/law surface dominates"
    if "Event" in secondary or "EventGuard" in guards:
        return "effect_trait_terminal_policy_with_event_guard", "", "effect_trait_terminal_policy", "terminal trait/accolade effect-list with event guard"
    if "Domain" in secondary or "DomainGuard" in guards:
        return "effect_trait_terminal_policy_with_domain_guard", "", "effect_trait_terminal_policy", "terminal trait/accolade effect-list with domain guard"
    if "DynamicToken" in secondary:
        return "needs_effect_trait_dynamic_parser_escape", "", "ck3_dynamic_symbolic_parser", "dynamic parser escape remains"
    if "EffectListGuard" in guards:
        return "effect_trait_terminal_policy_with_effect_list_guard", "", "effect_trait_terminal_policy", "terminal effect-list trait/accolade surface"
    return "effect_trait_blocked_uncertain", "", "human_review_or_evidence_collection", "insufficient trait/accolade evidence"


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_effect_list_trait_accolade_policy_review"
    spec = reports_dir / f"{stamp}_effect_list_trait_accolade_policy_spec.json"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), spec


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only effect-list trait/accolade policy review.")
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
    trait_counts: Counter[str] = Counter()
    accolade_counts: Counter[str] = Counter()
    knight_counts: Counter[str] = Counter()
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
        trait_markers = detect(TRAIT_MARKERS, blob)
        accolade_markers = detect(ACCOLADE_MARKERS, blob)
        knight_markers = detect(KNIGHT_MARKERS, blob)
        guard_markers = detect(GUARD_MARKERS, blob)
        secondary_markers = sorted(set(row.get("secondary_markers") or []) | set(detect(SECONDARY_MARKERS, blob)))
        decision, matched_policy, next_component, rationale = classify(
            state,
            registered,
            trait_markers,
            accolade_markers,
            knight_markers,
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
        trait_counts.update(trait_markers or ["NoTraitMarker"])
        accolade_counts.update(accolade_markers or ["NoAccoladeMarker"])
        knight_counts.update(knight_markers or ["NoKnightMarker"])
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
                "trait_markers": trait_markers,
                "accolade_markers": accolade_markers,
                "knight_markers": knight_markers,
                "matched_registered_policy": matched_policy,
                "guard_markers": guard_markers,
                "secondary_markers": secondary_markers,
                "effect_trait_decision": decision,
                "next_component": next_component,
                "requires_lifecycle_later": False,
                "requires_apply_later": False,
                "corrected_text": "",
                "rationale": rationale,
            }
        )

    reuse_count = sum(count for decision, count in decision_counts.items() if decision.startswith("effect_trait_reuse_"))
    terminal_count = sum(count for decision, count in decision_counts.items() if decision.startswith("effect_trait_terminal_policy"))
    dominant, dominant_count = decision_counts.most_common(1)[0] if decision_counts else ("none", 0)
    if reuse_count > len(results) / 2:
        next_prompt = "chat_exec_effect_list_trait_accolade_terminal_spec_registration_prompt.md"
        stop_rule = f"reuse_majority_terminal_readonly: {reuse_count}/{len(results)} reuse cataloged policies"
        policy_shape = "terminal_reuse_route"
    elif terminal_count > len(results) / 2:
        next_prompt = "chat_exec_effect_list_trait_accolade_terminal_spec_registration_prompt.md"
        stop_rule = f"terminal_majority_readonly: {terminal_count}/{len(results)} terminal policies"
        policy_shape = "terminal_policy"
    elif dominant.startswith("needs_") and dominant_count >= 12:
        next_prompt = f"chat_exec_{dominant.removeprefix('needs_')}_review_prompt.md"
        stop_rule = f"continue_narrow_prompt: {dominant} reached {dominant_count} >= 12"
        policy_shape = "splitter"
    else:
        next_prompt = "chat_exec_effect_list_script_value_policy_review_prompt.md"
        stop_rule = "fragmented_return_to_effect_list_script_value"
        policy_shape = "fragmented_splitter_guard"

    spec = {
        "schema_version": 1,
        "created_for": "read_only_subpolicy_design",
        "parent_policy": PARENT_POLICY,
        "policy_id": "effect_list_trait_accolade_policy",
        "segment_state_run_id": args.segment_state_run_id,
        "ledger_run_id": LEDGER_RUN_ID,
        "entry_conditions": [
            "effect_list_multiline_decision == needs_effect_list_trait_accolade_policy",
            "primary_route == effect_list_multiline_policy",
            "segment remains pending",
            "needs_output_apply == 0",
            "confirmed_matches_output == 1",
        ],
        "reused_registered_policies": [{"agent_key": key, "sampled": policy_counts.get(key, 0)} for key in sorted(registered & REGISTERED_POLICY_KEYS)],
        "reused_catalog_specs": [
            "get_trait_accolade_requirement_policy",
            "accolade_knight_attribute_policy",
            "knight_attribute_unlock_requirement_policy",
            "unlock_acclaimed_knight_entity_policy",
            "acclaimed_knight_entity_requirement_policy",
            "acclaimed_knight_entity_unlock_final_policy",
        ],
        "effect_trait_types": [{"decision": decision, "sampled": count} for decision, count in decision_counts.most_common()],
        "resolution_order": [
            "state guard",
            "residual guard",
            "acclaimed knight terminal chain",
            "accolade/knight attribute",
            "GetTrait/accolade",
            "ScriptValue",
            "gender/local-player",
            "artifact/activity",
            "concept/title-law",
            "event/domain",
        ],
        "next_components": [
            "get_trait_accolade_requirement_policy",
            "accolade_knight_attribute_policy",
            "knight_attribute_unlock_requirement_policy",
            "acclaimed_knight_entity_unlock_final_policy",
            "effect_list_script_value_policy",
        ],
        "blocked_conditions": ["state guard failed", "visible residual", "false trait/accolade marker from building/modifier/domain"],
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
            "trait_marker_counts": dict(trait_counts),
            "accolade_marker_counts": dict(accolade_counts),
            "knight_marker_counts": dict(knight_counts),
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
            handle.write(json.dumps({"record_type": "decision_count", "effect_trait_decision": decision, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for policy, count in policy_counts.most_common():
            handle.write(json.dumps({"record_type": "matched_policy_count", "matched_registered_policy": policy, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for counter_name, counter in [
            ("family_count", family_counts),
            ("effect_list_marker_count", effect_counts),
            ("trait_marker_count", trait_counts),
            ("accolade_marker_count", accolade_counts),
            ("knight_marker_count", knight_counts),
            ("guard_marker_count", guard_counts),
            ("secondary_marker_count", secondary_counts),
        ]:
            for marker, count in counter.most_common():
                handle.write(json.dumps({"record_type": counter_name, "marker": marker, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for priority, (prompt, rationale) in enumerate([
            (next_prompt, "selected by effect-list trait/accolade stop rule"),
            ("chat_exec_effect_list_script_value_policy_review_prompt.md", "next strong effect-list block if fragmented"),
            ("chat_exec_effect_list_policy_catalog_integration_prompt.md", "integrate partial effect-list package after more specs"),
        ], 1):
            handle.write(json.dumps({"record_type": "strategy", "priority": priority, "next_prompt": prompt, "rationale": rationale}, ensure_ascii=False, sort_keys=True) + "\n")
        for result in results:
            handle.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")

    with spec_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(spec, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("Effect-list trait/accolade policy review\n\n")
        handle.write(f"- total revisado: {len(results)}\n")
        handle.write(f"- reuso de policies/specs catalogadas: {reuse_count}\n")
        handle.write("- ready_lifecycle_future: 0\n")
        handle.write("- apply_candidates_future: 0\n")
        handle.write(f"- subtipo dominante: {dominant}\n")
        handle.write(f"- dominant_count: {dominant_count}\n")
        handle.write(f"- policy_shape: {policy_shape}\n")
        handle.write(f"- regra_de_parada: {stop_rule}\n")
        handle.write(f"- proximo_prompt: {next_prompt}\n\n")
        handle.write("effect_trait_decision_counts:\n")
        for decision, count in decision_counts.most_common():
            handle.write(f"- {decision}: {count}\n")
        handle.write("\nMatched registered policies:\n")
        for policy, count in policy_counts.most_common():
            handle.write(f"- {policy}: {count}\n")
        handle.write("\nTop families_open:\n")
        for family, count in family_counts.most_common(15):
            handle.write(f"- {family}: {count}\n")
        handle.write("\nTop effect-list markers:\n")
        for marker, count in effect_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nTop trait/accolade/knight markers:\n")
        for marker, count in (trait_counts + accolade_counts + knight_counts).most_common(20):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nTop guard markers:\n")
        for marker, count in guard_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nTop secondary markers:\n")
        for marker, count in secondary_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nAnalise\n")
        handle.write("- effect_list_trait_accolade_policy so deve virar terminal/reuso se a amostra provar maioria de reuso ou terminalidade.\n")
        handle.write("- Esta revisao nao gera lifecycle/apply.\n")
        handle.write("- Se fragmentar, encerrar este ramo e seguir para ScriptValue.\n")

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
