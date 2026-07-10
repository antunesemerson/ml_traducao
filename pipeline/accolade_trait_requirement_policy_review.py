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


PRIMARY_ROUTE = "accolade_trait_requirement_policy"
EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76
EXPECTED_UNIVERSE = 517
EXPECTED_REGISTERED_AGENTS = 222

REUSE_POLICIES = {
    "get_trait_accolade_requirement_policy",
    "accolade_knight_attribute_policy",
    "knight_attribute_unlock_requirement_policy",
    "unlock_acclaimed_knight_entity_policy",
    "acclaimed_knight_entity_requirement_policy",
    "acclaimed_knight_entity_unlock_final_policy",
    "effect_list_trait_accolade_policy",
}

SPEC_PATHS = {
    "scope_getter_requirement_policy": "reports/20260621_182252_832397_scope_getter_requirement_policy_spec.json",
    "get_trait_scope_requirement_policy": "reports/20260621_182856_093345_get_trait_scope_requirement_policy_spec.json",
    "get_trait_accolade_requirement_policy": "reports/20260621_183516_533002_get_trait_accolade_requirement_policy_spec.json",
    "accolade_knight_attribute_policy": "reports/20260621_184149_690696_accolade_knight_attribute_policy_spec.json",
    "knight_attribute_unlock_requirement_policy": "reports/20260621_184729_570490_knight_attribute_unlock_requirement_policy_spec.json",
    "unlock_acclaimed_knight_entity_policy": "reports/20260621_190000_627489_unlock_acclaimed_knight_entity_policy_spec.json",
    "acclaimed_knight_entity_requirement_policy": "reports/20260621_190448_336581_acclaimed_knight_entity_requirement_policy_spec.json",
    "acclaimed_knight_entity_unlock_final_policy": "reports/20260621_190820_254167_acclaimed_knight_entity_unlock_final_policy_spec.json",
    "effect_list_trait_accolade_policy": "reports/20260622_133901_719476_effect_list_trait_accolade_policy_spec.json",
}

TRAIT_MARKERS = [
    ("GetTrait", re.compile(r"GetTrait\(", re.I)),
    ("Trait", re.compile(r"\btrait\b|traits|tra[cç]o|trait_level_track|lifestyle_", re.I)),
    ("TraitList", re.compile(r"GetTrait\([^)]*\).+GetTrait\(", re.I | re.S)),
    ("GetAccoladeType", re.compile(r"GetAccoladeType\(", re.I)),
]

ACCOLADE_MARKERS = [
    ("Accolade", re.compile(r"accolade|accolade_type", re.I)),
    ("AcclaimedKnight", re.compile(r"acclaimed|acclaimed_knight", re.I)),
    ("AcclaimedKnightEntity", re.compile(r"acclaimed_knight|acclaimed knight|GetAcclaimedKnight|AccoladeType", re.I)),
]

KNIGHT_MARKERS = [
    ("Knight", re.compile(r"knight|cavaleir|cavalaria|prowess", re.I)),
    ("KnightAttribute", re.compile(r"attribute|aptitude|house_knight|besieger|charmer|disciplinarian|huntsmaster|idealist|lancer|mentor|stalwart|tactician|valiant", re.I)),
    ("MaA", re.compile(r"men_at_arms|maa|army|regiment|culture|tradition|innovation", re.I)),
]

REQUIREMENT_MARKERS = [
    ("Requirement", re.compile(r"requirement|requires|required|trigger|valid|allowed|cannot|can_|eligible|requisito", re.I)),
    ("Condition", re.compile(r"NO_CHANCE|invalid|valid|blocked|disabled|missing|has_|is_|not_", re.I)),
    ("Tooltip", re.compile(r"tooltip|_tt\b|#T\b|#help", re.I)),
]

UNLOCK_MARKERS = [
    ("Unlock", re.compile(r"unlock|desbloque|pode criar|pode se tornar|pode converter|pode ser tornado", re.I)),
    ("RequirementUnlock", re.compile(r"unlock.*require|require.*unlock|valid.*accolade|can_create|can_be", re.I | re.S)),
]

ACTIVITY_MARKERS = [
    ("Activity", re.compile(r"activity|tournament|travel|feast|hunt|pilgrimage|wedding|journey", re.I)),
    ("Tournament", re.compile(r"tournament|joust|melee|archery|contest", re.I)),
]

ACTOR_TARGET_MARKERS = [
    ("ActorTarget", re.compile(r"\bactor\b|\btarget\b|\brecipient\b|\baddressee\b|owner|liege|vassal", re.I)),
    ("ScopeGetter", re.compile(r"ROOT\.|FROM\.|SCOPE\.|TARGET\.|CHARACTER\.|Get[A-Za-z0-9_]+\(", re.I)),
]

GUARD_MARKERS = [
    ("DomainGuard", re.compile(r"culture|religion|faith|realm|title|law|government|succession|court", re.I)),
    ("EventGuard", re.compile(r"event|events/|\.desc|desc_|option|story|scheme|interaction|memory", re.I)),
    ("RequirementGuard", re.compile(r"requirement|requires|tooltip|trigger|condition|valid|eligible", re.I)),
]

SECONDARY_MARKERS = [
    ("ScriptValue", re.compile(r"ScriptValue|GetScriptValue|GetValue|\|V[0-9]?|\|=\+?0|[0-9]+\s*%", re.I)),
    ("EffectList", re.compile(r"\n|\\n|\$EFFECT_LIST_BULLET\$|#indent|#weak|#bold|#P|#N", re.I)),
    ("TitleLaw", re.compile(r"title|law|government|realm|succession|county|duchy|kingdom|empire|rank|holding", re.I)),
    ("NameDynasty", re.compile(r"name|nickname|dynasty|house|GetName|GetFirstName|GetDynasty|GetHouse|epithet", re.I)),
    ("Domain", re.compile(r"culture|religion|faith|doctrine|tradition|domain|realm", re.I)),
    ("Event", re.compile(r"event|events/|\.desc|desc_|option|toast|dialogue|story|scheme|interaction|memory", re.I)),
    ("ResidualVisible", re.compile(r"Ã|Â|�|â€™|â€œ|â€|\b(?:the|your|you|their|cannot|consiguio|sera|mas|facil)\b", re.I)),
    ("DynamicToken", re.compile(r"Custom\(|Select_CString|Concept\(|ScriptValue|GetTrait|GetAccoladeType|ROOT\.|FROM\.|SCOPE\.|TARGET\.|\[[^\]]+\]|\$[^$]+\$", re.I)),
]


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_accolade_trait_requirement_policy_review"
    spec = reports_dir / f"{stamp}_accolade_trait_requirement_policy_spec.json"
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
    if registry_count != EXPECTED_REGISTERED_AGENTS:
        raise SystemExit(f"registry guard failed: {registry_count} expected {EXPECTED_REGISTERED_AGENTS}")
    placeholders = ",".join("?" for _ in REUSE_POLICIES)
    rows = conn.execute(
        f"""
        SELECT agent_key
        FROM ml_agent_registry
        WHERE agent_key IN ({placeholders})
          AND status = 'active'
        """,
        tuple(sorted(REUSE_POLICIES)),
    ).fetchall()
    return {str(row["agent_key"]) for row in rows}


def catalog_specs() -> set[str]:
    present: set[str] = set()
    for key, rel_path in SPEC_PATHS.items():
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


def reuse_decision(policy: str, decision: str, registered: set[str], specs: set[str], rationale: str) -> tuple[str, str, str, str, str] | None:
    if policy in registered or policy in specs:
        return decision, policy if policy in registered else "", policy if policy in specs else "", policy, rationale
    return None


def classify(
    *,
    state: dict[str, Any],
    registered: set[str],
    specs: set[str],
    trait_markers: list[str],
    accolade_markers: list[str],
    knight_markers: list[str],
    requirement_markers: list[str],
    unlock_markers: list[str],
    activity_markers: list[str],
    actor_target_markers: list[str],
    guard_markers: list[str],
    secondary_markers: list[str],
) -> tuple[str, str, str, str, str]:
    trait = set(trait_markers)
    accolade = set(accolade_markers)
    knight = set(knight_markers)
    unlock = set(unlock_markers)
    activity = set(activity_markers)
    actor_target = set(actor_target_markers)
    guards = set(guard_markers)
    secondary = set(secondary_markers)
    if state["state_group"] != "pending" or int(state["is_closed"]) != 0:
        return "accolade_trait_blocked_uncertain", "", "", "state_guard", "segment is not pending in selected state run"
    if int(state["needs_output_apply"]) != 0 or int(state["confirmed_matches_output"]) != 1:
        return "accolade_trait_blocked_uncertain", "", "", "state_guard", "state guard failed: needs_output_apply or confirmed_matches_output"
    if "ResidualVisible" in secondary:
        return "needs_accolade_trait_residual_repair", "", "", "accolade_trait_residual_repair", "visible residual/mojibake remains"

    reuse_candidates = [
        ("effect_list_trait_accolade_policy", "accolade_trait_reuse_effect_list_trait_accolade_policy", "EffectList" in secondary, "effect-list trait/accolade surface can reuse registered effect-list policy"),
        ("acclaimed_knight_entity_unlock_final_policy", "accolade_trait_reuse_acclaimed_knight_unlock_final_policy", bool("AcclaimedKnightEntity" in accolade and unlock), "acclaimed knight unlock-final spec matches this requirement"),
        ("acclaimed_knight_entity_requirement_policy", "accolade_trait_reuse_acclaimed_knight_entity_policy", bool("AcclaimedKnightEntity" in accolade), "acclaimed knight entity requirement spec matches this requirement"),
        ("unlock_acclaimed_knight_entity_policy", "accolade_trait_reuse_acclaimed_knight_entity_policy", bool("AcclaimedKnight" in accolade and unlock), "unlock-acclaimed-knight entity spec matches this requirement"),
        ("knight_attribute_unlock_requirement_policy", "accolade_trait_reuse_knight_attribute_unlock_policy", bool("KnightAttribute" in knight and unlock), "knight attribute unlock requirement spec matches this requirement"),
        ("accolade_knight_attribute_policy", "accolade_trait_reuse_accolade_knight_attribute_policy", bool("KnightAttribute" in knight or "Knight" in knight), "accolade knight attribute spec matches this requirement"),
        ("get_trait_accolade_requirement_policy", "accolade_trait_reuse_get_trait_accolade_policy", bool("GetTrait" in trait or "GetAccoladeType" in trait or "Accolade" in accolade or "Trait" in trait), "GetTrait/accolade requirement spec matches this requirement"),
    ]
    for policy, decision, condition, rationale in reuse_candidates:
        if condition:
            reused = reuse_decision(policy, decision, registered, specs, rationale)
            if reused is not None:
                return reused

    if "ScopeGetter" in actor_target or "GetTrait" in trait:
        return "needs_accolade_trait_get_trait_scope_policy", "", "", "get_trait_scope_requirement_policy", "GetTrait/scope dependency remains"
    if "KnightAttribute" in knight or "Knight" in knight:
        return "needs_accolade_trait_knight_attribute_policy", "", "", "accolade_knight_attribute_policy", "knight attribute dependency remains"
    if "AcclaimedKnight" in accolade or "AcclaimedKnightEntity" in accolade:
        return "needs_accolade_trait_acclaimed_knight_policy", "", "", "acclaimed_knight_entity_requirement_policy", "acclaimed knight dependency remains"
    if unlock:
        return "needs_accolade_trait_unlock_requirement_policy", "", "", "knight_attribute_unlock_requirement_policy", "unlock requirement dependency remains"
    if "TraitList" in trait:
        return "needs_accolade_trait_trait_list_policy", "", "", "accolade_trait_list_policy", "multiple trait list dependency remains"
    if "MaA" in knight or "Domain" in secondary:
        return "needs_accolade_trait_maa_or_culture_policy", "", "", "accolade_trait_maa_or_culture_policy", "MaA/culture dependency remains"
    if activity:
        return "needs_accolade_trait_activity_condition_policy", "", "", "accolade_trait_activity_condition_policy", "activity condition dependency remains"
    if actor_target:
        return "needs_accolade_trait_actor_target_policy", "", "", "accolade_trait_actor_target_policy", "actor/target dependency remains"
    if "ScriptValue" in secondary:
        return "needs_accolade_trait_script_value_policy", "", "", "accolade_trait_script_value_policy", "ScriptValue dependency remains"
    if "EffectList" in secondary:
        return "needs_accolade_trait_effect_list_policy", "", "", "effect_list_trait_accolade_policy", "effect-list dependency remains"
    if "TitleLaw" in secondary:
        return "needs_accolade_trait_title_law_policy", "", "", "accolade_trait_title_law_policy", "title/law dependency remains"
    if "NameDynasty" in secondary:
        return "needs_accolade_trait_name_dynasty_policy", "", "", "accolade_trait_name_dynasty_policy", "name/dynasty dependency remains"
    if "DomainGuard" in guards:
        return "needs_accolade_trait_domain_context", "", "", "domain_context_after_requirement_effect", "domain context remains"
    if "Event" in secondary or "EventGuard" in guards:
        return "needs_accolade_trait_event_context", "", "", "event_context_after_requirement_effect", "event context remains"
    if "DynamicToken" in secondary:
        return "needs_accolade_trait_dynamic_parser_escape", "", "", "ck3_dynamic_symbolic_parser", "dynamic parser escape remains"
    if "DomainGuard" in guards:
        return "accolade_trait_terminal_policy_with_domain_guard", "", "", "accolade_trait_terminal_policy", "terminal accolade/trait requirement with domain guard"
    if "EventGuard" in guards:
        return "accolade_trait_terminal_policy_with_event_guard", "", "", "accolade_trait_terminal_policy", "terminal accolade/trait requirement with event guard"
    if trait or accolade or knight or requirement_markers:
        return "accolade_trait_terminal_policy", "", "", "accolade_trait_terminal_policy", "plain terminal accolade/trait requirement"
    return "accolade_trait_blocked_uncertain", "", "", "human_review_or_evidence_collection", "insufficient accolade/trait evidence"


def count_records(values: Counter[str], field: str) -> list[dict[str, Any]]:
    return [{"record_type": field, "value": key, "segments": count} for key, count in values.most_common()]


def build_spec(
    *,
    decision_counts: Counter[str],
    reused_policies: Counter[str],
    reused_specs: Counter[str],
    accolade_trait_counts: Counter[str],
    next_components: list[str],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "created_for": "read_only_subpolicy_design",
        "parent_policy": "requirement_effect_router_readonly",
        "policy_id": "accolade_trait_requirement_policy",
        "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
        "ledger_run_id": EXPECTED_LEDGER_RUN_ID,
        "entry_conditions": [
            "route == accolade_trait_requirement_policy",
            "segment remains pending",
            "needs_output_apply == 0",
            "confirmed_matches_output == 1",
        ],
        "reused_registered_policies": [{"agent_key": key, "sampled": count} for key, count in reused_policies.most_common()],
        "reused_catalog_specs": [{"policy_id": key, "sampled": count} for key, count in reused_specs.most_common()],
        "accolade_trait_types": [{"type": key, "sampled": count} for key, count in accolade_trait_counts.most_common()],
        "resolution_order": [
            "state guard",
            "visible residual guard",
            "effect-list trait/accolade reuse",
            "acclaimed knight entity and unlock reuse",
            "knight attribute reuse",
            "GetTrait/accolade reuse",
            "focused subpolicy handoff",
            "terminal guarded pattern",
        ],
        "next_components": next_components,
        "blocked_conditions": [
            "state guard failed",
            "visible residual",
            "mixed accolade/name/concept marker stack",
            "ambiguous CK3 dynamic structure",
        ],
        "promotion_gate": "read_only_component_only_no_apply_no_lifecycle",
        "observed_decision_counts": dict(decision_counts),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only accolade/trait requirement policy review.")
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
    trait_counts: Counter[str] = Counter()
    accolade_counts: Counter[str] = Counter()
    knight_counts: Counter[str] = Counter()
    requirement_counts: Counter[str] = Counter()
    unlock_counts: Counter[str] = Counter()
    activity_counts: Counter[str] = Counter()
    actor_target_counts: Counter[str] = Counter()
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
        trait_markers = detect(TRAIT_MARKERS, blob)
        accolade_markers = detect(ACCOLADE_MARKERS, blob)
        knight_markers = detect(KNIGHT_MARKERS, blob)
        requirement_markers = detect(REQUIREMENT_MARKERS, blob)
        unlock_markers = detect(UNLOCK_MARKERS, blob)
        activity_markers = detect(ACTIVITY_MARKERS, blob)
        actor_target_markers = detect(ACTOR_TARGET_MARKERS, blob)
        guard_markers = detect(GUARD_MARKERS, blob)
        secondary_markers = detect(SECONDARY_MARKERS, blob)
        decision, matched_registered_policy, matched_catalog_spec, next_component, rationale = classify(
            state=record["state"],
            registered=registered,
            specs=specs,
            trait_markers=trait_markers,
            accolade_markers=accolade_markers,
            knight_markers=knight_markers,
            requirement_markers=requirement_markers,
            unlock_markers=unlock_markers,
            activity_markers=activity_markers,
            actor_target_markers=actor_target_markers,
            guard_markers=guard_markers,
            secondary_markers=secondary_markers,
        )
        family_counts.update(record["families_open"])
        trait_counts.update(trait_markers)
        accolade_counts.update(accolade_markers)
        knight_counts.update(knight_markers)
        requirement_counts.update(requirement_markers)
        unlock_counts.update(unlock_markers)
        activity_counts.update(activity_markers)
        actor_target_counts.update(actor_target_markers)
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
                "trait_markers": trait_markers,
                "accolade_markers": accolade_markers,
                "knight_markers": knight_markers,
                "requirement_markers": requirement_markers,
                "unlock_markers": unlock_markers,
                "activity_markers": activity_markers,
                "actor_target_markers": actor_target_markers,
                "matched_registered_policy": matched_registered_policy,
                "matched_catalog_spec": matched_catalog_spec,
                "guard_markers": guard_markers,
                "secondary_markers": secondary_markers,
                "accolade_trait_decision": decision,
                "next_component": next_component,
                "requires_lifecycle_later": False,
                "requires_apply_later": False,
                "corrected_text": "",
                "rationale": rationale,
            }
        )

    reuse_total = sum(1 for row in results if row["matched_registered_policy"] or row["matched_catalog_spec"])
    terminal_total = sum(count for decision, count in decision_counts.items() if decision.startswith("accolade_trait_terminal"))
    dominant_decision, dominant_count = decision_counts.most_common(1)[0] if decision_counts else ("", 0)
    if dominant_decision.startswith("needs_") and dominant_count >= 50:
        next_prompt = f"chat_exec_{dominant_decision.removeprefix('needs_')}_review_prompt.md"
        recommendation = "open_narrow_subpolicy"
    elif reuse_total >= 70:
        next_prompt = "chat_exec_accolade_trait_requirement_policy_catalog_registration_prompt.md"
        recommendation = "register_readonly_reuse_splitter"
    elif terminal_total >= 70:
        next_prompt = "chat_exec_accolade_trait_requirement_terminal_spec_registration_prompt.md"
        recommendation = "register_terminal_readonly"
    else:
        next_prompt = "chat_exec_script_value_effect_policy_review_prompt.md"
        recommendation = "fragmented_move_to_next_large_route"

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
        accolade_trait_counts=trait_counts + accolade_counts + knight_counts + unlock_counts,
        next_components=[next_prompt],
    )

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(summary, ensure_ascii=False, sort_keys=True) + "\n")
        for decision, count in decision_counts.most_common():
            handle.write(json.dumps({"record_type": "decision_count", "accolade_trait_decision": decision, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for section_name, counts in [
            ("top_family", family_counts),
            ("top_trait_marker", trait_counts),
            ("top_accolade_marker", accolade_counts),
            ("top_knight_marker", knight_counts),
            ("top_requirement_marker", requirement_counts),
            ("top_unlock_marker", unlock_counts),
            ("top_activity_marker", activity_counts),
            ("top_actor_target_marker", actor_target_counts),
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
        handle.write("Accolade/trait requirement policy review\n\n")
        handle.write(f"- universo estimado: {universe}\n")
        handle.write(f"- total revisado: {len(results)}\n")
        handle.write(f"- reuso policies/specs catalogadas: {reuse_total}\n")
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
            ("Top trait markers", trait_counts),
            ("Top accolade markers", accolade_counts),
            ("Top knight markers", knight_counts),
            ("Top requirement/unlock markers", requirement_counts + unlock_counts),
            ("Top activity markers", activity_counts),
            ("Top actor/target markers", actor_target_counts),
            ("Top guard markers", guard_counts),
            ("Top secondary markers", secondary_counts),
        ]:
            handle.write(f"\n{title}\n")
            for key, count in counts.most_common(15):
                handle.write(f"- {key}: {count}\n")
        handle.write("\nRespostas\n")
        handle.write("- accolade_trait_requirement_policy deve virar componente read-only real se reuso >= 70 ou sublane dominante persistir.\n")
        handle.write("- lifecycle/apply em curto prazo: nao.\n")
        handle.write("- reaproveitamento medido contra a cadeia GetTrait/accolade/knight/acclaimed knight e effect-list trait/accolade.\n")
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
