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
import requirement_effect_router_readonly as router


PRIMARY_ROUTE = "event_context_after_requirement_effect"
EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76
EXPECTED_UNIVERSE = 692

REGISTERED_REUSE_POLICIES = {
    "artifact_activity_effect_policy",
    "building_modifier_effect_policy",
    "effect_list_artifact_activity_policy",
    "effect_list_gender_local_player_policy",
    "effect_list_trait_accolade_policy",
    "effect_list_script_value_policy",
    "effect_list_concept_policy",
}

CATALOG_REUSE_SPECS = {
    "artifact_activity_effect_policy",
    "building_modifier_effect_policy",
    "effect_list_artifact_activity_policy",
    "effect_list_gender_local_player_policy",
    "effect_list_trait_accolade_policy",
    "effect_list_script_value_policy",
    "effect_list_concept_policy",
}

EVENT_MARKERS = [
    ("EventFile", re.compile(r"events/|event|\.desc|desc_|option|flavor|story", re.I)),
    ("Interaction", re.compile(r"interaction|scheme|duel|challenge|petition|court_position", re.I)),
    ("EventOption", re.compile(r"\.a:|\.b:|\.c:|option|choice|accept|refuse", re.I)),
    ("EventTooltip", re.compile(r"_tt\b|\.tt\b|tooltip|#T|#help", re.I)),
]

REQUIREMENT_MARKERS = [
    ("Requirement", re.compile(r"requirement|requires|can_|allow|valid|eligible|unlock", re.I)),
    ("CannotMayMust", re.compile(r"cannot|can not|must|may not|allowed|available", re.I)),
    ("Trigger", re.compile(r"trigger|condition|has_|is_|needs_", re.I)),
]

EFFECT_MARKERS = [
    ("Effect", re.compile(r"effect|_effect_name\b|gain|lose|loss|add_|remove_", re.I)),
    ("Modifier", re.compile(r"modifier|opinion|prestige|piety|gold|stress", re.I)),
    ("EffectList", re.compile(r"\n|\\n|EFFECT_LIST_BULLET|BULLET_WITH_TAB", re.I)),
]

ACTIVITY_MARKERS = [
    ("Activity", re.compile(r"activity|activities/|activity_type", re.I)),
    ("Tournament", re.compile(r"tournament|contest|joust|melee|archery", re.I)),
    ("HuntFeastTravel", re.compile(r"\bhunt\b|feast|festival|travel|tour|journey|pilgrimage|wedding|funeral", re.I)),
]

ARTIFACT_MARKERS = [
    ("Artifact", re.compile(r"artifact|court_artifact|relic|inventory|antiquarian", re.I)),
    ("Item", re.compile(r"\bitem\b|weapon|armor|armou?r|book|trinket|regalia", re.I)),
]

ACTOR_TARGET_MARKERS = [
    ("ActorTarget", re.compile(r"actor|target|recipient|owner|liege|vassal|host|guest", re.I)),
    ("RootFromScope", re.compile(r"ROOT\.|FROM\.|SCOPE\.|TARGET\.|CHARACTER\.", re.I)),
    ("Getter", re.compile(r"Get[A-Za-z0-9_]+\(", re.I)),
]

GENDER_LOCAL_PLAYER_MARKERS = [
    ("SelectCString", re.compile(r"Select_CString", re.I)),
    ("ESHelper", re.compile(r"ES_(?:OA|XA|EA|ElLa|DelDela|AlAla|A|O)", re.I)),
    ("LocalPlayer", re.compile(r"local_player|GetPlayer|\bvoc(?:e|Ãª|ÃƒÂª)\b|\bseu\b|\bsua\b", re.I)),
]

DOMAIN_MARKERS = [
    ("TitleLaw", re.compile(r"title|county|duchy|kingdom|empire|law|succession|government|realm", re.I)),
    ("ReligionHolySite", re.compile(r"faith|religion|holy_site|holy site|church|temple|doctrine|piety", re.I)),
    ("CultureName", re.compile(r"culture|dynasty|nickname|house|name|GetName|GetDynasty", re.I)),
    ("DomainRealm", re.compile(r"domain|realm|court|council|liege|vassal", re.I)),
]

GUARD_MARKERS = [
    ("DomainGuard", re.compile(r"culture|faith|religion|realm|title|law|government|succession|church|court", re.I)),
    ("CharacterGuard", re.compile(r"character|trait|GetTrait|accolade|knight|prowess|skill|opinion", re.I)),
    ("EventGuard", re.compile(r"event|events|\.desc|desc_|option|flavor|story|scheme|interaction", re.I)),
    ("RequirementEffectGuard", re.compile(r"requirement|requires|effect|gain|loss|modifier|tooltip", re.I)),
]

SECONDARY_MARKERS = [
    ("ScriptValue", re.compile(r"ScriptValue|GetScriptValue|GetValue|\|V[0-9]?|\|=\+?0|[0-9]+\s*%", re.I)),
    ("Concept", re.compile(r"\[[A-Za-z0-9_]+\||Concept\(|game_concept", re.I)),
    ("TraitAccolade", re.compile(r"trait|GetTrait|accolade|knight|aptitude|prowess", re.I)),
    ("BuildingModifier", re.compile(r"building|holding|modifier|county_modifier|duchy_building", re.I)),
    ("ArtifactActivity", re.compile(r"artifact|activity|tournament|hunt|feast|travel|legend", re.I)),
    ("ResidualVisible", re.compile(r"ÃƒÆ’Ã†â€™|ÃƒÆ’Ã¢â‚¬Å¡|Ãƒâ€šÃ‚Â¿|Ãƒâ€šÃ‚Â¡|ÃƒÂ¢Ã¢â€šÂ¬|Ã¯Â¿Â½|\bthe\b|\byour\b|\byou\b|\btheir\b|\bcannot\b", re.I)),
    ("DynamicToken", re.compile(r"Custom\(|Select_CString|Concept\(|ScriptValue|GetTrait|ROOT\.|FROM\.|SCOPE\.|TARGET\.|\[[^\]]+\]|\$[^$]+\$", re.I)),
]

REQUIRED_SPEC_PATHS = {
    "effect_list_artifact_activity_policy": "reports/20260622_014609_044703_effect_list_artifact_activity_policy_spec.json",
    "artifact_activity_effect_policy": "reports/20260622_155336_452988_artifact_activity_effect_policy_spec.json",
    "building_modifier_effect_policy": "reports/20260622_161624_167472_building_modifier_effect_policy_spec.json",
    "effect_list_gender_local_player_policy": "reports/20260622_130623_420660_effect_list_gender_local_player_policy_spec.json",
    "effect_list_trait_accolade_policy": "reports/20260622_133901_719476_effect_list_trait_accolade_policy_spec.json",
    "effect_list_script_value_policy": "reports/20260622_141149_802441_effect_list_script_value_policy_spec.json",
    "effect_list_concept_policy": "reports/20260622_144059_266106_effect_list_concept_policy_spec.json",
}


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_requirement_effect_event_context_policy_review"
    spec = reports_dir / f"{stamp}_requirement_effect_event_context_policy_spec.json"
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


def detect(patterns: list[tuple[str, re.Pattern[str]]], blob: str) -> list[str]:
    return [name for name, pattern in patterns if pattern.search(blob)]


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
    registry_count = int(conn.execute("SELECT COUNT(*) AS c FROM ml_agent_registry").fetchone()["c"] or 0)
    if registry_count != 220:
        raise SystemExit(f"registry guard failed: {registry_count} expected 220")
    placeholders = ",".join("?" for _ in REGISTERED_REUSE_POLICIES)
    rows = conn.execute(
        f"""
        SELECT agent_key
        FROM ml_agent_registry
        WHERE agent_key IN ({placeholders})
          AND status = 'active'
        """,
        tuple(sorted(REGISTERED_REUSE_POLICIES)),
    ).fetchall()
    return {str(row["agent_key"]) for row in rows}


def catalog_specs() -> set[str]:
    present: set[str] = set()
    for key, rel_path in REQUIRED_SPEC_PATHS.items():
        path = Path.cwd() / rel_path
        if not path.exists():
            continue
        json.loads(path.read_text(encoding="utf-8"))
        present.add(key)
    return present


def route_records(conn: sqlite3.Connection, segment_state_run_id: int, ledger_run_id: int) -> list[dict[str, Any]]:
    router.fetch_runs(conn, segment_state_run_id, ledger_run_id)
    grouped = router.fetch_pending_rows(conn, segment_state_run_id, ledger_run_id)
    records: list[dict[str, Any]] = []
    for segment_id, rows in grouped.items():
        first = rows[0]
        blob = router.blob_for(rows)
        markers = router.detect_markers(blob)
        route, _reason = router.route_for(blob, markers)
        if route != PRIMARY_ROUTE:
            continue
        records.append(
            {
                "segment_id": segment_id,
                "relative_path": str(first.get("relative_path") or ""),
                "source_key": str(first.get("source_key") or ""),
                "families_open": list(router.families_for(rows)),
                "router_markers": markers,
                "state": {
                    "state_group": first.get("state_group"),
                    "is_closed": int(first.get("is_closed") or 0),
                    "needs_output_apply": int(first.get("needs_output_apply") or 0),
                    "confirmed_matches_output": int(first.get("confirmed_matches_output") or 0),
                },
            }
        )
    records.sort(key=lambda row: (row["relative_path"], row["source_key"], int(row["segment_id"])))
    return records


def reuse_decision(
    policy: str,
    decision: str,
    registered: set[str],
    specs: set[str],
    rationale: str,
) -> tuple[str, str, str, str, str] | None:
    if policy in registered or policy in specs:
        matched_registered = policy if policy in registered else ""
        matched_spec = policy if policy in specs else ""
        return decision, matched_registered, matched_spec, policy, rationale
    return None


def classify(
    *,
    state: dict[str, Any],
    registered: set[str],
    specs: set[str],
    event_markers: list[str],
    requirement_markers: list[str],
    effect_markers: list[str],
    activity_markers: list[str],
    artifact_markers: list[str],
    actor_target_markers: list[str],
    gender_local_player_markers: list[str],
    domain_markers: list[str],
    guard_markers: list[str],
    secondary_markers: list[str],
) -> tuple[str, str, str, str, str]:
    events = set(event_markers)
    requirements = set(requirement_markers)
    effects = set(effect_markers)
    activities = set(activity_markers)
    artifacts = set(artifact_markers)
    actors = set(actor_target_markers)
    gender = set(gender_local_player_markers)
    domains = set(domain_markers)
    guards = set(guard_markers)
    secondary = set(secondary_markers)
    if state["state_group"] != "pending" or int(state["is_closed"]) != 0:
        return "event_context_blocked_uncertain", "", "", "state_guard", "segment is not pending in selected state run"
    if int(state["needs_output_apply"]) != 0 or int(state["confirmed_matches_output"]) != 1:
        return "event_context_blocked_uncertain", "", "", "state_guard", "state guard failed: needs_output_apply or confirmed_matches_output"

    if "ResidualVisible" in secondary:
        return "needs_event_context_residual_repair", "", "", "event_context_residual_repair", "visible residual remains"

    for candidate in [
        ("artifact_activity_effect_policy", "event_context_reuse_artifact_activity_effect_policy", bool("ArtifactActivity" in secondary or activities or artifacts), "artifact/activity event context can reuse registered splitter"),
        ("building_modifier_effect_policy", "event_context_reuse_building_modifier_effect_policy", bool("BuildingModifier" in secondary), "building/modifier event context can reuse registered splitter"),
        ("effect_list_artifact_activity_policy", "event_context_reuse_effect_list_artifact_activity_policy", bool((activities or artifacts) and "EffectList" in effects), "effect-list artifact/activity event context can reuse cataloged policy"),
        ("effect_list_gender_local_player_policy", "event_context_reuse_effect_list_gender_local_player_policy", bool(gender), "gender/local-player event context can reuse effect-list terminal guard"),
        ("effect_list_trait_accolade_policy", "event_context_reuse_effect_list_trait_accolade_policy", bool("TraitAccolade" in secondary), "trait/accolade event context can reuse effect-list policy"),
        ("effect_list_script_value_policy", "event_context_reuse_effect_list_script_value_policy", bool("ScriptValue" in secondary), "ScriptValue event context can reuse effect-list policy"),
        ("effect_list_concept_policy", "event_context_reuse_effect_list_concept_policy", bool("Concept" in secondary), "concept event context can reuse effect-list policy"),
    ]:
        policy, decision, condition, rationale = candidate
        if condition:
            reused = reuse_decision(policy, decision, registered, specs, rationale)
            if reused is not None:
                return reused

    if activities:
        return "needs_event_context_activity_policy", "", "", "event_context_activity_policy", "activity/tournament/travel event context remains"
    if artifacts:
        return "needs_event_context_artifact_policy", "", "", "event_context_artifact_policy", "artifact/item event context remains"
    if "Interaction" in events:
        return "needs_event_context_character_interaction_policy", "", "", "event_context_character_interaction_policy", "character interaction event context remains"
    if actors:
        return "needs_event_context_actor_target_policy", "", "", "event_context_actor_target_policy", "actor/target/scope event context remains"
    if gender:
        return "needs_event_context_gender_local_player_policy", "", "", "event_context_gender_local_player_policy", "gender/local-player event context remains"
    if "TitleLaw" in domains:
        return "needs_event_context_title_law_policy", "", "", "event_context_title_law_policy", "title/law event context remains"
    if "ReligionHolySite" in domains:
        return "needs_event_context_religion_or_holy_site_policy", "", "", "event_context_religion_or_holy_site_policy", "religion/holy-site event context remains"
    if "CultureName" in domains:
        return "needs_event_context_culture_or_name_policy", "", "", "event_context_culture_or_name_policy", "culture/name event context remains"
    if "TraitAccolade" in secondary:
        return "needs_event_context_accolade_trait_policy", "", "", "event_context_accolade_trait_policy", "accolade/trait event context remains"
    if "ScriptValue" in secondary:
        return "needs_event_context_script_value_policy", "", "", "event_context_script_value_policy", "ScriptValue event context remains"
    if "RootFromScope" in actors or "Getter" in actors:
        return "needs_event_context_scope_getter_policy", "", "", "event_context_scope_getter_policy", "scope/getter event context remains"
    if "DomainGuard" in guards or "DomainRealm" in domains:
        return "needs_event_context_domain_context", "", "", "domain_context_after_requirement_effect", "domain guard remains"
    if "DynamicToken" in secondary:
        return "needs_event_context_dynamic_parser_escape", "", "", "ck3_dynamic_symbolic_parser", "dynamic parser escape remains"
    if "CharacterGuard" in guards:
        return "event_context_terminal_policy_with_character_guard", "", "", "event_context_terminal_policy", "terminal event context with character guard"
    if "DomainGuard" in guards:
        return "event_context_terminal_policy_with_domain_guard", "", "", "event_context_terminal_policy", "terminal event context with domain guard"
    if events and (requirements or effects):
        return "event_context_terminal_policy", "", "", "event_context_terminal_policy", "plain terminal event context requirement/effect pattern"
    return "event_context_blocked_uncertain", "", "", "human_review_or_evidence_collection", "insufficient event context evidence"


def count_records(values: Counter[str], field: str) -> list[dict[str, Any]]:
    return [{"record_type": field, "value": key, "segments": count} for key, count in values.most_common()]


def build_spec(
    *,
    decision_counts: Counter[str],
    reused_policies: Counter[str],
    reused_specs: Counter[str],
    event_type_counts: Counter[str],
    next_components: list[str],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "created_for": "read_only_subpolicy_design",
        "parent_policy": "requirement_effect_router_readonly",
        "policy_id": "event_context_after_requirement_effect",
        "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
        "ledger_run_id": EXPECTED_LEDGER_RUN_ID,
        "entry_conditions": [
            "route == event_context_after_requirement_effect",
            "segment remains pending",
            "needs_output_apply == 0",
            "confirmed_matches_output == 1",
        ],
        "reused_registered_policies": [{"agent_key": key, "sampled": count} for key, count in reused_policies.most_common()],
        "reused_catalog_specs": [{"policy_id": key, "sampled": count} for key, count in reused_specs.most_common()],
        "event_context_types": [{"type": key, "sampled": count} for key, count in event_type_counts.most_common()],
        "resolution_order": [
            "state guard",
            "visible residual guard",
            "reuse artifact/activity and building/modifier packages",
            "reuse effect-list terminal/splitter policies",
            "activity/artifact/character interaction sublanes",
            "actor-target/gender/title/domain sublanes",
            "dynamic parser escape",
            "terminal event context",
        ],
        "next_components": next_components,
        "blocked_conditions": [
            "state guard failed",
            "visible residual",
            "insufficient event context evidence",
        ],
        "promotion_gate": "read_only_component_only_no_apply_no_lifecycle",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only event context after requirement/effect policy review.")
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    parser.add_argument("--ledger-run-id", required=True, type=int)
    parser.add_argument("--limit", type=int, default=240)
    args = parser.parse_args()
    if args.segment_state_run_id != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("segment_state_run_id guard failed")
    if args.ledger_run_id != EXPECTED_LEDGER_RUN_ID:
        raise SystemExit("ledger_run_id guard failed")

    specs = catalog_specs()
    with connect_readonly() as conn:
        records = route_records(conn, args.segment_state_run_id, args.ledger_run_id)
        registered = registered_policies(conn)
        selected = records[: min(args.limit, 240)]
        texts = fetch_texts(conn, [int(row["segment_id"]) for row in selected])

    universe = len(records)
    if universe != EXPECTED_UNIVERSE:
        raise SystemExit(f"universe guard failed: {universe} expected {EXPECTED_UNIVERSE}")

    results: list[dict[str, Any]] = []
    decision_counts: Counter[str] = Counter()
    family_counts: Counter[str] = Counter()
    event_counts: Counter[str] = Counter()
    requirement_counts: Counter[str] = Counter()
    effect_counts: Counter[str] = Counter()
    activity_counts: Counter[str] = Counter()
    artifact_counts: Counter[str] = Counter()
    actor_target_counts: Counter[str] = Counter()
    gender_counts: Counter[str] = Counter()
    domain_counts: Counter[str] = Counter()
    guard_counts: Counter[str] = Counter()
    secondary_counts: Counter[str] = Counter()
    reused_policy_counts: Counter[str] = Counter()
    reused_spec_counts: Counter[str] = Counter()

    for record in selected:
        segment_id = int(record["segment_id"])
        text = texts.get(segment_id, {"old_text": "", "confirmed_text": "", "output_text": ""})
        blob = " ".join(
            [
                record["relative_path"],
                record["source_key"],
                text["old_text"],
                text["confirmed_text"],
                text["output_text"],
                " ".join(record["families_open"]),
            ]
        )
        event_markers = detect(EVENT_MARKERS, blob)
        requirement_markers = detect(REQUIREMENT_MARKERS, blob)
        effect_markers = detect(EFFECT_MARKERS, blob)
        activity_markers = detect(ACTIVITY_MARKERS, blob)
        artifact_markers = detect(ARTIFACT_MARKERS, blob)
        actor_target_markers = detect(ACTOR_TARGET_MARKERS, blob)
        gender_local_player_markers = detect(GENDER_LOCAL_PLAYER_MARKERS, blob)
        domain_markers = detect(DOMAIN_MARKERS, blob)
        guard_markers = detect(GUARD_MARKERS, blob)
        secondary_markers = detect(SECONDARY_MARKERS, blob)
        decision, matched_registered_policy, matched_catalog_spec, next_component, rationale = classify(
            state=record["state"],
            registered=registered,
            specs=specs,
            event_markers=event_markers,
            requirement_markers=requirement_markers,
            effect_markers=effect_markers,
            activity_markers=activity_markers,
            artifact_markers=artifact_markers,
            actor_target_markers=actor_target_markers,
            gender_local_player_markers=gender_local_player_markers,
            domain_markers=domain_markers,
            guard_markers=guard_markers,
            secondary_markers=secondary_markers,
        )
        family_counts.update(record["families_open"])
        event_counts.update(event_markers)
        requirement_counts.update(requirement_markers)
        effect_counts.update(effect_markers)
        activity_counts.update(activity_markers)
        artifact_counts.update(artifact_markers)
        actor_target_counts.update(actor_target_markers)
        gender_counts.update(gender_local_player_markers)
        domain_counts.update(domain_markers)
        guard_counts.update(guard_markers)
        secondary_counts.update(secondary_markers)
        decision_counts[decision] += 1
        if matched_registered_policy:
            reused_policy_counts[matched_registered_policy] += 1
        if matched_catalog_spec:
            reused_spec_counts[matched_catalog_spec] += 1
        results.append(
            {
                "record_type": "sample_review",
                "segment_id": segment_id,
                "relative_path": record["relative_path"],
                "source_key": record["source_key"],
                "families_open": record["families_open"],
                "primary_route": PRIMARY_ROUTE,
                "old_text": text["old_text"],
                "confirmed_text": text["confirmed_text"],
                "output_text": text["output_text"],
                "event_markers": event_markers,
                "requirement_markers": requirement_markers,
                "effect_markers": effect_markers,
                "activity_markers": activity_markers,
                "artifact_markers": artifact_markers,
                "actor_target_markers": actor_target_markers,
                "gender_local_player_markers": gender_local_player_markers,
                "domain_markers": domain_markers,
                "matched_registered_policy": matched_registered_policy,
                "matched_catalog_spec": matched_catalog_spec,
                "guard_markers": guard_markers,
                "secondary_markers": secondary_markers,
                "event_context_decision": decision,
                "next_component": next_component,
                "requires_lifecycle_later": False,
                "requires_apply_later": False,
                "corrected_text": "",
                "rationale": rationale,
            }
        )

    reuse_total = sum(1 for row in results if row["matched_registered_policy"] or row["matched_catalog_spec"])
    terminal_total = sum(count for decision, count in decision_counts.items() if decision.startswith("event_context_terminal"))
    dominant_decision, dominant_count = decision_counts.most_common(1)[0] if decision_counts else ("", 0)
    if dominant_decision.startswith("needs_") and dominant_count >= 50:
        next_prompt = f"chat_exec_{dominant_decision.removeprefix('needs_')}_review_prompt.md"
        recommendation = "open_narrow_subpolicy"
    elif reuse_total >= 70:
        next_prompt = "chat_exec_requirement_effect_event_context_policy_catalog_registration_prompt.md"
        recommendation = "register_readonly_reuse_splitter"
    elif terminal_total >= 70:
        next_prompt = "chat_exec_requirement_effect_event_context_terminal_spec_registration_prompt.md"
        recommendation = "register_terminal_readonly"
    else:
        next_prompt = "chat_exec_requirement_effect_residual_review_prompt.md"
        recommendation = "fragmented_move_to_residual_route"

    txt_path, jsonl_path, spec_path = output_paths()
    summary = {
        "record_type": "summary",
        "segment_state_run_id": args.segment_state_run_id,
        "ledger_run_id": args.ledger_run_id,
        "universe_estimated": universe,
        "total_reviewed": len(results),
        "decision_counts": dict(decision_counts),
        "reused_cataloged_policy_count": reuse_total,
        "reused_registered_policy_count": sum(reused_policy_counts.values()),
        "reused_catalog_spec_count": sum(reused_spec_counts.values()),
        "terminal_policy_count": terminal_total,
        "dominant_subtype": dominant_decision,
        "dominant_count": dominant_count,
        "ready_lifecycle_future": 0,
        "apply_candidates_future": 0,
        "requires_lifecycle_later": False,
        "requires_apply_later": False,
        "recommendation": recommendation,
        "next_prompt": next_prompt,
        "policy_shape": "reuse_splitter_route" if reuse_total >= 70 else "splitter_candidate",
    }

    spec = build_spec(
        decision_counts=decision_counts,
        reused_policies=reused_policy_counts,
        reused_specs=reused_spec_counts,
        event_type_counts=event_counts + activity_counts + artifact_counts + domain_counts,
        next_components=[next_prompt],
    )

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(summary, ensure_ascii=False, sort_keys=True) + "\n")
        for decision, count in decision_counts.most_common():
            handle.write(json.dumps({"record_type": "decision_count", "event_context_decision": decision, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for section_name, counts in [
            ("top_family", family_counts),
            ("top_event_marker", event_counts),
            ("top_requirement_marker", requirement_counts),
            ("top_effect_marker", effect_counts),
            ("top_activity_marker", activity_counts),
            ("top_artifact_marker", artifact_counts),
            ("top_actor_target_marker", actor_target_counts),
            ("top_gender_local_player_marker", gender_counts),
            ("top_domain_marker", domain_counts),
            ("top_guard_marker", guard_counts),
            ("top_secondary_marker", secondary_counts),
        ]:
            for row in count_records(counts, section_name):
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        for result in results:
            handle.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")

    with spec_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(spec, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("Requirement/effect event context policy review\n\n")
        handle.write(f"- universo estimado: {universe}\n")
        handle.write(f"- total revisado: {len(results)}\n")
        handle.write(f"- reuso policies/specs catalogadas: {reuse_total}\n")
        handle.write(f"- reuso registry: {sum(reused_policy_counts.values())}\n")
        handle.write(f"- reuso specs: {sum(reused_spec_counts.values())}\n")
        handle.write(f"- terminal policies futuras: {terminal_total}\n")
        handle.write("- ready lifecycle futuro: 0\n")
        handle.write("- apply candidates futuro: 0\n")
        handle.write(f"- dominante: {dominant_decision} ({dominant_count})\n")
        handle.write(f"- recomendacao: {recommendation}\n")
        handle.write(f"- proximo prompt: {next_prompt}\n\n")
        handle.write("Decisoes\n")
        for decision, count in decision_counts.most_common():
            handle.write(f"- {decision}: {count}\n")
        for title, counts in [
            ("Top families_open", family_counts),
            ("Top event markers", event_counts),
            ("Top requirement markers", requirement_counts),
            ("Top effect markers", effect_counts),
            ("Top activity/artifact markers", activity_counts + artifact_counts),
            ("Top actor/target markers", actor_target_counts),
            ("Top gender/local-player markers", gender_counts),
            ("Top domain markers", domain_counts),
            ("Top guard markers", guard_counts),
            ("Top secondary markers", secondary_counts),
        ]:
            handle.write(f"\n{title}\n")
            for key, count in counts.most_common(15):
                handle.write(f"- {key}: {count}\n")
        handle.write("\nRespostas\n")
        handle.write("- event_context_after_requirement_effect deve virar componente read-only real: sim, se o reuso catalogado ou sublane dominante se mantiver.\n")
        handle.write("- lifecycle/apply em curto prazo: nao.\n")
        handle.write("- reaproveitamento medido contra effect-list, artifact/activity e building/modifier: ver contagens de reuso acima.\n")
        handle.write(f"- proximo prompt recomendado: {next_prompt}\n")
        handle.write("- sem escrita em banco, source ou output.\n")

    print(f"txt: {txt_path}")
    print(f"jsonl: {jsonl_path}")
    print(f"spec: {spec_path}")
    print(f"universe_estimated: {universe}")
    print(f"total_reviewed: {len(results)}")
    print(f"reused_cataloged_policy_count: {reuse_total}")
    print(f"terminal_policy_count: {terminal_total}")
    print(f"dominant_subtype: {dominant_decision}")
    print(f"dominant_count: {dominant_count}")
    print(f"next_prompt: {next_prompt}")


if __name__ == "__main__":
    main()
