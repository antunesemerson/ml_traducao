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


SOURCE_DECISION = "needs_artifact_activity_gender_local_player_policy"
PRIMARY_ROUTE = "effect_list_multiline_policy"
PARENT_POLICY = "effect_list_artifact_activity_policy"
LEDGER_RUN_ID = 76

REGISTERED_POLICY_KEYS = {
    "select_cstring_player_target_direct_policy",
    "select_cstring_possessive_policy",
    "select_cstring_es_helper_policy",
    "local_player_requirement_policy",
    "es_oa_requirement_policy",
}

ALLOWED_DECISIONS = {
    "artifact_gender_reuse_select_cstring_player_target_policy",
    "artifact_gender_reuse_select_cstring_possessive_policy",
    "artifact_gender_reuse_select_cstring_es_helper_policy",
    "artifact_gender_reuse_local_player_requirement_policy",
    "artifact_gender_terminal_policy_with_activity_guard",
    "artifact_gender_terminal_policy_with_artifact_guard",
    "needs_artifact_gender_activity_perspective_policy",
    "needs_artifact_gender_actor_target_policy",
    "needs_artifact_gender_custom_loc_policy",
    "needs_artifact_gender_script_value_policy",
    "needs_artifact_gender_event_context",
    "needs_artifact_gender_domain_context",
    "needs_artifact_gender_residual_repair",
    "needs_artifact_gender_dynamic_parser_escape",
    "artifact_gender_blocked_uncertain",
}

ARTIFACT_MARKERS = [
    ("Artifact", re.compile(r"artifact|commission_artifact|inspired|inspiration|legend_library", re.I)),
]

ACTIVITY_MARKERS = [
    ("Activity", re.compile(r"activity|activities/|ActivityWindow|GetActivityType", re.I)),
    ("Hunt", re.compile(r"hunt", re.I)),
    ("Tournament", re.compile(r"tournament|contest", re.I)),
    ("Travel", re.compile(r"travel|adventurer|pilgrimage", re.I)),
    ("Education", re.compile(r"education|examination", re.I)),
    ("FestivalWedding", re.compile(r"festival|wedding|feast|funeral", re.I)),
]

GENDER_MARKERS = [
    ("SelectCString", re.compile(r"Select_CString", re.I)),
    ("ESHelper", re.compile(r"Custom\('ES_(?:OA|XA|EA|ElLa|DelDela|AlAla|A|O)'\)|ES_(?:OA|XA|EA|ElLa|DelDela|AlAla|A|O)", re.I)),
    ("PronounGetter", re.compile(r"GetSheHe|GetHerHim|GetHerHis|GetHerselfHimself|GetWomanMan|GetLadyLord", re.I)),
    ("GenderedCustomLoc", re.compile(r"\.Custom\('(?:GetBrideGroom|GetWritingMaterial|FormOfAddressForLiege)'\)", re.I)),
]

LOCAL_PLAYER_MARKERS = [
    ("GetPlayer", re.compile(r"GetPlayer", re.I)),
    ("SecondPerson", re.compile(r"\b[Vv]ocê\b|\bseu\b|\bsua\b|\bseus\b|\bsuas\b|\bmeu\b|\bminha\b|\bmim\b|\bme\b", re.I)),
    ("PlayerScope", re.compile(r"ROOT\.Char|root_scope", re.I)),
]

GUARD_MARKERS = [
    ("ActivityGuard", re.compile(r"activity|hunt|tournament|contest|travel|pilgrimage|education|wedding|festival|ActivityWindow|GetActivityType", re.I)),
    ("ArtifactGuard", re.compile(r"artifact|inspired|inspiration|legend_library", re.I)),
    ("EventGuard", re.compile(r"event|events|\.desc|desc_|flavor|spectator|qualify|recital", re.I)),
    ("DomainGuard", re.compile(r"domain|faith|religion|legend|kurultai|building|barony|county|posse|holding", re.I)),
]

SECONDARY_MARKERS = [
    ("Possessive", re.compile(r"\bseu\b|\bsua\b|\bseus\b|\bsuas\b|\bmeu\b|\bminha\b", re.I)),
    ("SelectCString", re.compile(r"Select_CString", re.I)),
    ("ESHelper", re.compile(r"ES_(?:OA|XA|EA|ElLa|DelDela|AlAla|A|O)", re.I)),
    ("LocalPlayer", re.compile(r"GetPlayer|\b[Vv]ocê\b|\bseu\b|\bsua\b|\bmeu\b|\bminha\b|ROOT\.Char", re.I)),
    ("ActorTarget", re.compile(r"\bactor\.|\btarget\.|\brecipient\.|\bchronicler\.|\bhost\.|\bguest\.|\bstudent_[0-9]\.|\bspouse_[0-9]\.|\bvictim\.|\bcontest_winner\.|\blifestyle_character\.|\bmysterious_stranger\.", re.I)),
    ("CustomLoc", re.compile(r"\.Custom\(|Custom\(", re.I)),
    ("ScriptValue", re.compile(r"ScriptValue|GetScriptValue|GetSpecialProgressValue|\|V[0-9]?|#V\s*[0-9]+|[0-9]+\s*%", re.I)),
    ("Event", re.compile(r"event|events|\.desc|desc_|flavor|spectator|qualify|recital", re.I)),
    ("Domain", re.compile(r"domain|faith|religion|legend|kurultai|building|barony|county|posse|holding|activity", re.I)),
    ("ResidualVisible", re.compile(r"ÃƒÆ’|Ãƒâ€š|Ã‚Â¿|Ã‚Â¡|Ã¢â‚¬â„¢|Ã¢â‚¬Å“|Ã¢â‚¬ï¿½|ï¿½|Estudios decentes|nivel|artefactos|\bthe\b|\byour\b|\byou\b|\btheir\b|\bcannot\b", re.I)),
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
        and row.get("artifact_activity_decision") == SOURCE_DECISION
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
    gender_markers: list[str],
    local_player_markers: list[str],
    secondary_markers: list[str],
) -> tuple[str, str] | None:
    gender = set(gender_markers)
    local_player = set(local_player_markers)
    secondary = set(secondary_markers)
    if "SelectCString" in gender or "SelectCString" in secondary:
        if "Possessive" in secondary and "select_cstring_possessive_policy" in registered:
            return "artifact_gender_reuse_select_cstring_possessive_policy", "select_cstring_possessive_policy"
        if "ESHelper" in secondary and "select_cstring_es_helper_policy" in registered:
            return "artifact_gender_reuse_select_cstring_es_helper_policy", "select_cstring_es_helper_policy"
        if "select_cstring_player_target_direct_policy" in registered:
            return "artifact_gender_reuse_select_cstring_player_target_policy", "select_cstring_player_target_direct_policy"
    if "ESHelper" in gender or "ESHelper" in secondary:
        if "select_cstring_es_helper_policy" in registered:
            return "artifact_gender_reuse_select_cstring_es_helper_policy", "select_cstring_es_helper_policy"
        if "es_oa_requirement_policy" in registered:
            return "artifact_gender_reuse_select_cstring_es_helper_policy", "es_oa_requirement_policy"
    if local_player or "LocalPlayer" in secondary:
        if "local_player_requirement_policy" in registered:
            return "artifact_gender_reuse_local_player_requirement_policy", "local_player_requirement_policy"
    return None


def classify(
    state: dict[str, Any] | None,
    registered: set[str],
    artifact_markers: list[str],
    activity_markers: list[str],
    gender_markers: list[str],
    local_player_markers: list[str],
    guard_markers: list[str],
    secondary_markers: list[str],
) -> tuple[str, str, str, str]:
    guards = set(guard_markers)
    secondary = set(secondary_markers)
    if not state or state["state_group"] != "pending" or int(state["is_closed"] or 0) != 0:
        return "artifact_gender_blocked_uncertain", "", "state_guard", "segment is not pending in selected state run"
    if int(state["needs_output_apply"] or 0) != 0 or int(state["confirmed_matches_output"] or 0) != 1:
        return "artifact_gender_blocked_uncertain", "", "state_guard", "state guard failed: needs_output_apply or confirmed_matches_output"
    if "ResidualVisible" in secondary:
        return "needs_artifact_gender_residual_repair", "", "residual_dependency_filtered_repair", "visible residual remains"

    reuse = choose_reuse_policy(registered, gender_markers, local_player_markers, secondary_markers)
    if reuse:
        decision, policy = reuse
        return decision, policy, policy, f"can reuse registered policy {policy} with artifact/activity guard"

    if "ScriptValue" in secondary:
        return "needs_artifact_gender_script_value_policy", "", "artifact_gender_script_value_policy", "ScriptValue/numeric dependency dominates"
    if "ActorTarget" in secondary:
        return "needs_artifact_gender_actor_target_policy", "", "artifact_gender_actor_target_policy", "actor/target or named-scope gender dependency dominates"
    if "CustomLoc" in secondary:
        return "needs_artifact_gender_custom_loc_policy", "", "artifact_gender_custom_loc_policy", "CustomLoc gender dependency dominates"
    if "Event" in secondary or "EventGuard" in guards:
        return "needs_artifact_gender_event_context", "", "artifact_gender_event_context_policy", "event context dominates"
    if "Domain" in secondary or "DomainGuard" in guards:
        return "needs_artifact_gender_domain_context", "", "artifact_gender_domain_context_policy", "domain context dominates"
    if "DynamicToken" in secondary:
        return "needs_artifact_gender_dynamic_parser_escape", "", "ck3_dynamic_symbolic_parser", "dynamic parser escape remains"
    if activity_markers:
        return "artifact_gender_terminal_policy_with_activity_guard", "", "artifact_gender_terminal_policy", "local artifact/activity gender surface with activity guard"
    if artifact_markers:
        return "artifact_gender_terminal_policy_with_artifact_guard", "", "artifact_gender_terminal_policy", "local artifact/activity gender surface with artifact guard"
    return "artifact_gender_blocked_uncertain", "", "human_review_or_evidence_collection", "insufficient artifact/activity gender evidence"


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_artifact_activity_gender_local_player_policy_review"
    spec = reports_dir / f"{stamp}_artifact_activity_gender_local_player_policy_spec.json"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), spec


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only artifact/activity gender-local-player policy review.")
    parser.add_argument("--artifact-activity-jsonl", required=True, type=Path)
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    args = parser.parse_args()

    rows = source_rows(args.artifact_activity_jsonl)
    segment_ids = [int(row["segment_id"]) for row in rows]
    conn = connect_readonly()
    states = fetch_states(conn, args.segment_state_run_id, segment_ids)
    texts = fetch_texts(conn, segment_ids)
    registered = registered_policies(conn)

    results: list[dict[str, Any]] = []
    decision_counts: Counter[str] = Counter()
    policy_counts: Counter[str] = Counter()
    family_counts: Counter[str] = Counter()
    artifact_counts: Counter[str] = Counter()
    activity_counts: Counter[str] = Counter()
    gender_counts: Counter[str] = Counter()
    local_player_counts: Counter[str] = Counter()
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
                " ".join(row.get("artifact_markers") or []),
                " ".join(row.get("activity_markers") or []),
                " ".join(row.get("secondary_markers") or []),
            ]
        )

        artifact_markers = sorted(set(row.get("artifact_markers") or []) | set(detect(ARTIFACT_MARKERS, blob)))
        activity_markers = sorted(set(row.get("activity_markers") or []) | set(detect(ACTIVITY_MARKERS, blob)))
        gender_markers = detect(GENDER_MARKERS, blob)
        local_player_markers = detect(LOCAL_PLAYER_MARKERS, blob)
        guard_markers = detect(GUARD_MARKERS, blob)
        secondary_markers = sorted(set(row.get("secondary_markers") or []) | set(detect(SECONDARY_MARKERS, blob)))
        decision, matched_policy, next_component, rationale = classify(
            state,
            registered,
            artifact_markers,
            activity_markers,
            gender_markers,
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
        artifact_counts.update(artifact_markers or ["NoArtifactMarker"])
        activity_counts.update(activity_markers or ["NoActivityMarker"])
        gender_counts.update(gender_markers or ["NoGenderMarker"])
        local_player_counts.update(local_player_markers or ["NoLocalPlayerMarker"])
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
                "artifact_markers": artifact_markers,
                "activity_markers": activity_markers,
                "gender_markers": gender_markers,
                "local_player_markers": local_player_markers,
                "matched_registered_policy": matched_policy,
                "guard_markers": guard_markers,
                "secondary_markers": secondary_markers,
                "artifact_gender_decision": decision,
                "next_component": next_component,
                "requires_lifecycle_later": False,
                "requires_apply_later": False,
                "corrected_text": "",
                "rationale": rationale,
            }
        )

    reuse_count = sum(count for decision, count in decision_counts.items() if decision.startswith("artifact_gender_reuse_"))
    dominant, dominant_count = decision_counts.most_common(1)[0] if decision_counts else ("none", 0)
    if reuse_count > len(results) / 2:
        next_prompt = "chat_exec_artifact_activity_gender_local_player_terminal_spec_registration_prompt.md"
        stop_rule = f"reuse_majority_terminal_readonly: {reuse_count}/{len(results)} reuse registered policies"
    elif dominant.startswith("needs_") and dominant_count >= 10:
        next_prompt = f"chat_exec_{dominant.removeprefix('needs_')}_review_prompt.md"
        stop_rule = f"continue_narrow_prompt: {dominant} reached {dominant_count} >= 10"
    else:
        next_prompt = "chat_exec_artifact_activity_script_value_policy_review_prompt.md"
        stop_rule = "fragmented_return_to_artifact_activity_script_value"

    spec = {
        "schema_version": 1,
        "created_for": "read_only_subpolicy_design",
        "parent_policy": PARENT_POLICY,
        "policy_id": "artifact_activity_gender_local_player_policy",
        "segment_state_run_id": args.segment_state_run_id,
        "ledger_run_id": LEDGER_RUN_ID,
        "entry_conditions": [
            "artifact_activity_decision == needs_artifact_activity_gender_local_player_policy",
            "primary_route == effect_list_multiline_policy",
            "segment remains pending",
            "needs_output_apply == 0",
            "confirmed_matches_output == 1",
        ],
        "reused_registered_policies": [{"agent_key": key, "sampled": policy_counts.get(key, 0)} for key in sorted(registered & REGISTERED_POLICY_KEYS)],
        "artifact_gender_types": [{"decision": decision, "sampled": count} for decision, count in decision_counts.most_common()],
        "resolution_order": [
            "state guard",
            "residual guard",
            "Select_CString possessive/player-target",
            "ES helpers",
            "local-player direct",
            "ScriptValue/numeric",
            "actor/target",
            "CustomLoc",
            "event/domain",
        ],
        "next_components": [
            "select_cstring_player_target_direct_policy",
            "select_cstring_possessive_policy",
            "select_cstring_es_helper_policy",
            "local_player_requirement_policy",
            "artifact_activity_script_value_policy",
        ],
        "blocked_conditions": ["state guard failed", "visible residual", "missing registered policy", "ambiguous artifact/activity gender surface"],
        "promotion_gate": "read_only_reuse_policy_only_no_apply_no_lifecycle",
        "sampled": len(results),
        "reuse_registered_policy_count": reuse_count,
        "reuse_registered_policy_majority": reuse_count > len(results) / 2,
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
            "artifact_marker_counts": dict(artifact_counts),
            "activity_marker_counts": dict(activity_counts),
            "gender_marker_counts": dict(gender_counts),
            "local_player_marker_counts": dict(local_player_counts),
            "guard_marker_counts": dict(guard_counts),
            "secondary_marker_counts": dict(secondary_counts),
            "reuse_registered_policy_count": reuse_count,
            "ready_lifecycle_future": 0,
            "apply_candidates_future": 0,
            "dominant_subtype": dominant,
            "dominant_count": dominant_count,
            "stop_rule": stop_rule,
            "next_prompt": next_prompt,
            "requires_lifecycle_later": False,
            "requires_apply_later": False,
        }, ensure_ascii=False, sort_keys=True) + "\n")
        for decision, count in decision_counts.most_common():
            handle.write(json.dumps({"record_type": "decision_count", "artifact_gender_decision": decision, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for policy, count in policy_counts.most_common():
            handle.write(json.dumps({"record_type": "matched_policy_count", "matched_registered_policy": policy, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for counter_name, counter in [
            ("family_count", family_counts),
            ("artifact_marker_count", artifact_counts),
            ("activity_marker_count", activity_counts),
            ("gender_marker_count", gender_counts),
            ("local_player_marker_count", local_player_counts),
            ("guard_marker_count", guard_counts),
            ("secondary_marker_count", secondary_counts),
        ]:
            for marker, count in counter.most_common():
                handle.write(json.dumps({"record_type": counter_name, "marker": marker, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for priority, (prompt, rationale) in enumerate([
            (next_prompt, "selected by artifact gender stop rule"),
            ("chat_exec_artifact_activity_script_value_policy_review_prompt.md", "return to sibling branch if reuse does not terminalize"),
            ("chat_exec_requirement_effect_router_post_policy_diagnostic_prompt.md", "global check after effect-list subpolicy expansion"),
        ], 1):
            handle.write(json.dumps({"record_type": "strategy", "priority": priority, "next_prompt": prompt, "rationale": rationale}, ensure_ascii=False, sort_keys=True) + "\n")
        for result in results:
            handle.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")

    with spec_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(spec, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("Artifact/activity gender-local-player policy review\n\n")
        handle.write(f"- total revisado: {len(results)}\n")
        handle.write(f"- reuso de policies registradas: {reuse_count}\n")
        handle.write("- ready_lifecycle_future: 0\n")
        handle.write("- apply_candidates_future: 0\n")
        handle.write(f"- subtipo dominante: {dominant}\n")
        handle.write(f"- dominant_count: {dominant_count}\n")
        handle.write(f"- regra_de_parada: {stop_rule}\n")
        handle.write(f"- proximo_prompt: {next_prompt}\n\n")
        handle.write("artifact_gender_decision_counts:\n")
        for decision, count in decision_counts.most_common():
            handle.write(f"- {decision}: {count}\n")
        handle.write("\nMatched registered policies:\n")
        for policy, count in policy_counts.most_common():
            handle.write(f"- {policy}: {count}\n")
        handle.write("\nTop families_open:\n")
        for family, count in family_counts.most_common(15):
            handle.write(f"- {family}: {count}\n")
        handle.write("\nTop artifact/activity markers:\n")
        for marker, count in (artifact_counts + activity_counts).most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nTop gender/local-player markers:\n")
        for marker, count in (gender_counts + local_player_counts).most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nTop guard markers:\n")
        for marker, count in guard_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nTop secondary markers:\n")
        for marker, count in secondary_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nAnalise\n")
        handle.write("- artifact_activity_gender_local_player_policy deve virar rota read-only de reuso se as policies registradas forem maioria.\n")
        handle.write("- Esta revisao nao gera lifecycle/apply.\n")
        handle.write("- Registro futuro como policy filha de effect_list_artifact_activity_policy deve expor o reuso de terminal policies com guard artifact/activity.\n")

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
