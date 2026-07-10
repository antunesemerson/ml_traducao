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


SOURCE_DECISION = "needs_effect_list_gender_local_player_policy"
PRIMARY_ROUTE = "effect_list_multiline_policy"
PARENT_POLICY = "effect_list_multiline_policy"
LEDGER_RUN_ID = 76

REGISTERED_POLICY_KEYS = {
    "select_cstring_player_target_direct_policy",
    "select_cstring_possessive_policy",
    "select_cstring_es_helper_policy",
    "local_player_requirement_policy",
    "es_oa_requirement_policy",
    "artifact_activity_gender_local_player_policy",
}

ALLOWED_DECISIONS = {
    "effect_gender_reuse_select_cstring_player_target_policy",
    "effect_gender_reuse_select_cstring_possessive_policy",
    "effect_gender_reuse_select_cstring_es_helper_policy",
    "effect_gender_reuse_local_player_requirement_policy",
    "effect_gender_reuse_artifact_activity_gender_policy",
    "effect_gender_terminal_policy_with_effect_list_guard",
    "effect_gender_terminal_policy_with_event_guard",
    "effect_gender_terminal_policy_with_domain_guard",
    "needs_effect_gender_artifact_activity_policy",
    "needs_effect_gender_trait_accolade_policy",
    "needs_effect_gender_script_value_policy",
    "needs_effect_gender_actor_target_policy",
    "needs_effect_gender_custom_loc_policy",
    "needs_effect_gender_event_context",
    "needs_effect_gender_domain_context",
    "needs_effect_gender_residual_repair",
    "needs_effect_gender_dynamic_parser_escape",
    "effect_gender_blocked_uncertain",
}

EFFECT_LIST_MARKERS = [
    ("Multiline", re.compile(r"\n|Multiline|EFFECT_LIST_BULLET|BULLET_WITH_TAB|\\n", re.I)),
    ("Tooltip", re.compile(r"tooltip|_tt\b|\.tt\b|#T|#help", re.I)),
    ("EffectListBullet", re.compile(r"EFFECT_LIST_BULLET|BULLET_WITH_TAB|\$BULLET", re.I)),
]

GENDER_MARKERS = [
    ("SelectCString", re.compile(r"Select_CString", re.I)),
    ("ESHelper", re.compile(r"ES_(?:OA|XA|EA|ElLa|DelDela|AlAla|A|O)", re.I)),
    ("PronounGetter", re.compile(r"GetSheHe|GetHerHim|GetHerHis|GetHerselfHimself|GetWomanMan|GetLadyLord", re.I)),
    ("GenderedCustomLoc", re.compile(r"\.Custom\(|Custom2?\(", re.I)),
]

LOCAL_PLAYER_MARKERS = [
    ("GetPlayer", re.compile(r"GetPlayer", re.I)),
    ("SecondPerson", re.compile(r"\b[Vv]ocê\b|\bseu\b|\bsua\b|\bseus\b|\bsuas\b|\bteu\b|\btua\b|\bmeu\b|\bminha\b|\bmim\b|\bme\b", re.I)),
    ("PlayerScope", re.compile(r"ROOT\.Char|root_scope|Province\.Self", re.I)),
]

SELECT_CSTRING_MARKERS = [
    ("SelectCString", re.compile(r"Select_CString", re.I)),
    ("SelectCStringFemale", re.compile(r"Select_CString\([^)]*IsFemale", re.I)),
]

ES_HELPER_MARKERS = [
    ("ES_OA", re.compile(r"ES_OA", re.I)),
    ("ES_XA", re.compile(r"ES_XA", re.I)),
    ("ES_ElLa", re.compile(r"ES_ElLa", re.I)),
    ("ES_DelDela", re.compile(r"ES_DelDela", re.I)),
    ("ES_AlAla", re.compile(r"ES_AlAla", re.I)),
]

GUARD_MARKERS = [
    ("EffectListGuard", re.compile(r"\n|Multiline|EFFECT_LIST_BULLET|BULLET_WITH_TAB|tooltip|_tt\b|\.tt\b|#T|#help", re.I)),
    ("ArtifactActivityGuard", re.compile(r"ArtifactActivity|artifact|activity|tournament|hunt|travel|pilgrimage|survey|tour", re.I)),
    ("TraitAccoladeGuard", re.compile(r"TraitAccolade|AccoladeTrait|GetTrait|trait|accolade|knight", re.I)),
    ("ScriptValueGuard", re.compile(r"ScriptValue|GetScriptValue|Subtract_CFixedPoint|GetValue|\|V[0-9]?|#V|[0-9]+\s*%", re.I)),
    ("EventGuard", re.compile(r"event|events|\.desc|desc_|flavor|intro|secret|victim|childhood|court|yearly", re.I)),
    ("DomainGuard", re.compile(r"Concept|Domain|game_concept|faith|religion|realm|government|council|hostage|regency|province", re.I)),
]

SECONDARY_MARKERS = [
    ("ArtifactActivity", re.compile(r"ArtifactActivity|artifact|activity|tournament|hunt|travel|pilgrimage|survey|tour", re.I)),
    ("TraitAccolade", re.compile(r"TraitAccolade|AccoladeTrait|GetTrait|trait|accolade|knight", re.I)),
    ("ScriptValue", re.compile(r"ScriptValue|GetScriptValue|Subtract_CFixedPoint|GetValue|\|V[0-9]?|#V|[0-9]+\s*%", re.I)),
    ("Possessive", re.compile(r"\bseu\b|\bsua\b|\bseus\b|\bsuas\b|\bmeu\b|\bminha\b|\bteu\b|\btua\b", re.I)),
    ("SelectCString", re.compile(r"Select_CString", re.I)),
    ("ESHelper", re.compile(r"ES_(?:OA|XA|EA|ElLa|DelDela|AlAla|A|O)", re.I)),
    ("LocalPlayer", re.compile(r"GetPlayer|\b[Vv]ocê\b|\bseu\b|\bsua\b|\bmeu\b|\bminha\b|ROOT\.Char|Province\.Self", re.I)),
    ("ActorTarget", re.compile(r"\bactor\.|\btarget\.|\brecipient\.|\bhost\.|\bguest\.|\bemployer\.|\bbg_loser\.|\bdead_emperor\.|\bvictim\.|\bbishop\.|\bhostage\.|\bROOT\.Char|\bSCOPE\.", re.I)),
    ("CustomLoc", re.compile(r"\.Custom\(|Custom2?\(", re.I)),
    ("Event", re.compile(r"event|events|\.desc|desc_|flavor|intro|secret|victim|childhood|court|yearly", re.I)),
    ("Domain", re.compile(r"Concept|Domain|game_concept|faith|religion|realm|government|council|hostage|regency|province", re.I)),
    ("ResidualVisible", re.compile(r"ÃƒÆ’|Ãƒâ€š|Ã‚Â¿|Ã‚Â¡|Ã¢â‚¬â„¢|Ã¢â‚¬Å“|Ã¢â‚¬ï¿½|ï¿½|nuestra|nuestro|Maravilloso|INACEPTABLE|\bthe\b|\byour\b|\byou\b|\btheir\b|\bcannot\b", re.I)),
    ("DynamicToken", re.compile(r"Custom\(|Select_CString|Concept\(|ScriptValue|GetTrait|GetActivityType|ROOT\.|FROM\.|SCOPE\.|TARGET\.|\[[^\]]+\]|\$[^$]+\$|dynamic_ck3_expression|dynamictoken", re.I)),
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
    select_markers: list[str],
    es_markers: list[str],
    local_player_markers: list[str],
    secondary_markers: list[str],
) -> tuple[str, str] | None:
    select = set(select_markers)
    es = set(es_markers)
    local_player = set(local_player_markers)
    secondary = set(secondary_markers)
    if select or "SelectCString" in secondary:
        if "Possessive" in secondary and "select_cstring_possessive_policy" in registered:
            return "effect_gender_reuse_select_cstring_possessive_policy", "select_cstring_possessive_policy"
        if (es or "ESHelper" in secondary) and "select_cstring_es_helper_policy" in registered:
            return "effect_gender_reuse_select_cstring_es_helper_policy", "select_cstring_es_helper_policy"
        if "select_cstring_player_target_direct_policy" in registered:
            return "effect_gender_reuse_select_cstring_player_target_policy", "select_cstring_player_target_direct_policy"
    if es or "ESHelper" in secondary:
        if "select_cstring_es_helper_policy" in registered:
            return "effect_gender_reuse_select_cstring_es_helper_policy", "select_cstring_es_helper_policy"
        if "es_oa_requirement_policy" in registered:
            return "effect_gender_reuse_select_cstring_es_helper_policy", "es_oa_requirement_policy"
    if local_player or "LocalPlayer" in secondary:
        if "local_player_requirement_policy" in registered:
            return "effect_gender_reuse_local_player_requirement_policy", "local_player_requirement_policy"
    if "ArtifactActivity" in secondary and "artifact_activity_gender_local_player_policy" in registered:
        return "effect_gender_reuse_artifact_activity_gender_policy", "artifact_activity_gender_local_player_policy"
    return None


def classify(
    state: dict[str, Any] | None,
    registered: set[str],
    select_markers: list[str],
    es_markers: list[str],
    local_player_markers: list[str],
    guard_markers: list[str],
    secondary_markers: list[str],
) -> tuple[str, str, str, str]:
    guards = set(guard_markers)
    secondary = set(secondary_markers)
    if not state or state["state_group"] != "pending" or int(state["is_closed"] or 0) != 0:
        return "effect_gender_blocked_uncertain", "", "state_guard", "segment is not pending in selected state run"
    if int(state["needs_output_apply"] or 0) != 0 or int(state["confirmed_matches_output"] or 0) != 1:
        return "effect_gender_blocked_uncertain", "", "state_guard", "state guard failed: needs_output_apply or confirmed_matches_output"
    if "ResidualVisible" in secondary:
        return "needs_effect_gender_residual_repair", "", "residual_dependency_filtered_repair", "visible residual remains"

    reuse = choose_reuse_policy(registered, select_markers, es_markers, local_player_markers, secondary_markers)
    if reuse:
        decision, policy = reuse
        return decision, policy, policy, f"can reuse registered policy {policy} with effect-list guard"

    if "ScriptValue" in secondary or "ScriptValueGuard" in guards:
        return "needs_effect_gender_script_value_policy", "", "effect_gender_script_value_policy", "ScriptValue dependency dominates"
    if "TraitAccolade" in secondary or "TraitAccoladeGuard" in guards:
        return "needs_effect_gender_trait_accolade_policy", "", "effect_gender_trait_accolade_policy", "trait/accolade dependency dominates"
    if "ArtifactActivity" in secondary or "ArtifactActivityGuard" in guards:
        return "needs_effect_gender_artifact_activity_policy", "", "effect_gender_artifact_activity_policy", "artifact/activity dependency dominates"
    if "ActorTarget" in secondary:
        return "needs_effect_gender_actor_target_policy", "", "effect_gender_actor_target_policy", "actor/target or named-scope gender dependency dominates"
    if "CustomLoc" in secondary:
        return "needs_effect_gender_custom_loc_policy", "", "effect_gender_custom_loc_policy", "CustomLoc gender dependency dominates"
    if "Event" in secondary or "EventGuard" in guards:
        return "needs_effect_gender_event_context", "", "effect_gender_event_context_policy", "event context dominates"
    if "Domain" in secondary or "DomainGuard" in guards:
        return "needs_effect_gender_domain_context", "", "effect_gender_domain_context_policy", "domain context dominates"
    if "DynamicToken" in secondary:
        return "needs_effect_gender_dynamic_parser_escape", "", "ck3_dynamic_symbolic_parser", "dynamic parser escape remains"
    if "EffectListGuard" in guards:
        return "effect_gender_terminal_policy_with_effect_list_guard", "", "effect_gender_terminal_policy", "terminal effect-list gender surface"
    return "effect_gender_blocked_uncertain", "", "human_review_or_evidence_collection", "insufficient effect-list gender evidence"


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_effect_list_gender_local_player_policy_review"
    spec = reports_dir / f"{stamp}_effect_list_gender_local_player_policy_spec.json"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), spec


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only effect-list gender/local-player policy review.")
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
    gender_counts: Counter[str] = Counter()
    local_player_counts: Counter[str] = Counter()
    select_counts: Counter[str] = Counter()
    es_counts: Counter[str] = Counter()
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
        gender_markers = detect(GENDER_MARKERS, blob)
        local_player_markers = detect(LOCAL_PLAYER_MARKERS, blob)
        select_cstring_markers = detect(SELECT_CSTRING_MARKERS, blob)
        es_helper_markers = detect(ES_HELPER_MARKERS, blob)
        guard_markers = detect(GUARD_MARKERS, blob)
        secondary_markers = sorted(set(row.get("secondary_markers") or []) | set(detect(SECONDARY_MARKERS, blob)))
        decision, matched_policy, next_component, rationale = classify(
            state,
            registered,
            select_cstring_markers,
            es_helper_markers,
            local_player_markers,
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
        gender_counts.update(gender_markers or ["NoGenderMarker"])
        local_player_counts.update(local_player_markers or ["NoLocalPlayerMarker"])
        select_counts.update(select_cstring_markers or ["NoSelectCStringMarker"])
        es_counts.update(es_helper_markers or ["NoESHelperMarker"])
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
                "gender_markers": gender_markers,
                "local_player_markers": local_player_markers,
                "select_cstring_markers": select_cstring_markers,
                "es_helper_markers": es_helper_markers,
                "matched_registered_policy": matched_policy,
                "guard_markers": guard_markers,
                "secondary_markers": secondary_markers,
                "effect_gender_decision": decision,
                "next_component": next_component,
                "requires_lifecycle_later": False,
                "requires_apply_later": False,
                "corrected_text": "",
                "rationale": rationale,
            }
        )

    reuse_count = sum(count for decision, count in decision_counts.items() if decision.startswith("effect_gender_reuse_"))
    dominant, dominant_count = decision_counts.most_common(1)[0] if decision_counts else ("none", 0)
    if reuse_count > len(results) / 2:
        next_prompt = "chat_exec_effect_list_gender_local_player_terminal_spec_registration_prompt.md"
        stop_rule = f"reuse_majority_terminal_readonly: {reuse_count}/{len(results)} reuse registered policies"
        policy_shape = "terminal_reuse_route"
    elif dominant.startswith("needs_") and dominant_count >= 15:
        next_prompt = f"chat_exec_{dominant.removeprefix('needs_')}_review_prompt.md"
        stop_rule = f"continue_narrow_prompt: {dominant} reached {dominant_count} >= 15"
        policy_shape = "splitter"
    else:
        next_prompt = "chat_exec_effect_list_trait_accolade_policy_review_prompt.md"
        stop_rule = "fragmented_return_to_effect_list_trait_accolade"
        policy_shape = "fragmented_splitter_guard"

    spec = {
        "schema_version": 1,
        "created_for": "read_only_subpolicy_design",
        "parent_policy": PARENT_POLICY,
        "policy_id": "effect_list_gender_local_player_policy",
        "segment_state_run_id": args.segment_state_run_id,
        "ledger_run_id": LEDGER_RUN_ID,
        "entry_conditions": [
            "effect_list_multiline_decision == needs_effect_list_gender_local_player_policy",
            "primary_route == effect_list_multiline_policy",
            "segment remains pending",
            "needs_output_apply == 0",
            "confirmed_matches_output == 1",
        ],
        "reused_registered_policies": [{"agent_key": key, "sampled": policy_counts.get(key, 0)} for key in sorted(registered & REGISTERED_POLICY_KEYS)],
        "effect_gender_types": [{"decision": decision, "sampled": count} for decision, count in decision_counts.most_common()],
        "resolution_order": [
            "state guard",
            "residual guard",
            "Select_CString possessive/player-target",
            "ES helpers",
            "local-player direct",
            "artifact/activity registered policy",
            "ScriptValue",
            "trait/accolade",
            "actor/target and CustomLoc",
            "event/domain",
        ],
        "next_components": [
            "select_cstring_player_target_direct_policy",
            "select_cstring_possessive_policy",
            "select_cstring_es_helper_policy",
            "local_player_requirement_policy",
            "artifact_activity_gender_local_player_policy",
            "effect_list_trait_accolade_policy",
        ],
        "blocked_conditions": ["state guard failed", "visible residual", "missing registered policy", "ambiguous effect-list gender surface"],
        "promotion_gate": "read_only_component_only_no_apply_no_lifecycle",
        "sampled": len(results),
        "reuse_registered_policy_count": reuse_count,
        "reuse_registered_policy_majority": reuse_count > len(results) / 2,
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
            "gender_marker_counts": dict(gender_counts),
            "local_player_marker_counts": dict(local_player_counts),
            "select_cstring_marker_counts": dict(select_counts),
            "es_helper_marker_counts": dict(es_counts),
            "guard_marker_counts": dict(guard_counts),
            "secondary_marker_counts": dict(secondary_counts),
            "reuse_registered_policy_count": reuse_count,
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
            handle.write(json.dumps({"record_type": "decision_count", "effect_gender_decision": decision, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for policy, count in policy_counts.most_common():
            handle.write(json.dumps({"record_type": "matched_policy_count", "matched_registered_policy": policy, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for counter_name, counter in [
            ("family_count", family_counts),
            ("effect_list_marker_count", effect_counts),
            ("gender_marker_count", gender_counts),
            ("local_player_marker_count", local_player_counts),
            ("select_cstring_marker_count", select_counts),
            ("es_helper_marker_count", es_counts),
            ("guard_marker_count", guard_counts),
            ("secondary_marker_count", secondary_counts),
        ]:
            for marker, count in counter.most_common():
                handle.write(json.dumps({"record_type": counter_name, "marker": marker, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for priority, (prompt, rationale) in enumerate([
            (next_prompt, "selected by effect-list gender stop rule"),
            ("chat_exec_effect_list_trait_accolade_policy_review_prompt.md", "next strong effect-list block"),
            ("chat_exec_global_post_architecture_diagnostic_prompt.md", "global check after effect-list expansion"),
        ], 1):
            handle.write(json.dumps({"record_type": "strategy", "priority": priority, "next_prompt": prompt, "rationale": rationale}, ensure_ascii=False, sort_keys=True) + "\n")
        for result in results:
            handle.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")

    with spec_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(spec, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("Effect-list gender/local-player policy review\n\n")
        handle.write(f"- total revisado: {len(results)}\n")
        handle.write(f"- reuso de policies registradas: {reuse_count}\n")
        handle.write("- ready_lifecycle_future: 0\n")
        handle.write("- apply_candidates_future: 0\n")
        handle.write(f"- subtipo dominante: {dominant}\n")
        handle.write(f"- dominant_count: {dominant_count}\n")
        handle.write(f"- policy_shape: {policy_shape}\n")
        handle.write(f"- regra_de_parada: {stop_rule}\n")
        handle.write(f"- proximo_prompt: {next_prompt}\n\n")
        handle.write("effect_gender_decision_counts:\n")
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
        handle.write("\nTop gender/local-player markers:\n")
        for marker, count in (gender_counts + local_player_counts).most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nTop Select_CString/ES helper markers:\n")
        for marker, count in (select_counts + es_counts).most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nTop guard markers:\n")
        for marker, count in guard_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nTop secondary markers:\n")
        for marker, count in secondary_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nAnalise\n")
        handle.write("- effect_list_gender_local_player_policy deve virar componente read-only real como rota de reuso se reuso for maioria.\n")
        handle.write("- Esta revisao nao gera lifecycle/apply.\n")
        handle.write("- Registro deve aguardar a decisao do pacote effect-list, salvo se a regra de parada pedir terminal spec agora.\n")

    print(f"txt: {txt_path}")
    print(f"jsonl: {jsonl_path}")
    print(f"spec: {spec_path}")
    print(f"total_reviewed: {len(results)}")
    print(f"reuse_registered_policy_count: {reuse_count}")
    print("ready_lifecycle_future: 0")
    print("apply_candidates_future: 0")
    print(f"stop_rule: {stop_rule}")
    print("decision_counts:")
    for decision, count in decision_counts.most_common():
        print(f"  {decision}: {count}")


if __name__ == "__main__":
    main()
