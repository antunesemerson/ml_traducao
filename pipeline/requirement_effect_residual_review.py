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


PRIMARY_ROUTE = "residual_repair_after_requirement_effect"
EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76
EXPECTED_UNIVERSE = 575
EXPECTED_REGISTERED_AGENTS = 221

REGISTERED_REUSE_POLICIES = {
    "effect_list_gender_local_player_policy",
    "effect_list_script_value_policy",
    "effect_list_concept_policy",
    "effect_list_trait_accolade_policy",
    "artifact_activity_effect_policy",
    "building_modifier_effect_policy",
    "event_context_after_requirement_effect",
}

REQUIRED_SPEC_PATHS = {
    "effect_list_gender_local_player_policy": "reports/20260622_130623_420660_effect_list_gender_local_player_policy_spec.json",
    "effect_list_script_value_policy": "reports/20260622_141149_802441_effect_list_script_value_policy_spec.json",
    "effect_list_concept_policy": "reports/20260622_144059_266106_effect_list_concept_policy_spec.json",
    "effect_list_trait_accolade_policy": "reports/20260622_133901_719476_effect_list_trait_accolade_policy_spec.json",
    "artifact_activity_effect_policy": "reports/20260622_155336_452988_artifact_activity_effect_policy_spec.json",
    "building_modifier_effect_policy": "reports/20260622_161624_167472_building_modifier_effect_policy_spec.json",
    "event_context_after_requirement_effect": "reports/20260622_171258_175723_requirement_effect_event_context_policy_spec.json",
}

RESIDUAL_MARKERS = [
    ("VisibleEncoding", re.compile(r"Ã|Â|�|â€™|â€œ|â€|Ãƒ|Ã‚")),
    ("EnglishResidue", re.compile(r"\b(the|your|you|their|cannot|must|may not|will|has|have)\b", re.I)),
    ("SpanishResidue", re.compile(r"\b(el|la|los|las|debe|puede|tiene|obtiene|pierde)\b|[¿¡]", re.I)),
    ("PunctuationSpacing", re.compile(r"\s+[,.!?;:]|[({\[]\s+|\s+[)}\]]")),
]

LANGUAGE_RESIDUAL_MARKERS = [
    ("MojibakePTBR", re.compile(r"Ã(?:£|©|ª|§|Ã)|Ãƒ|Ã‚|Â")),
    ("EnglishWords", re.compile(r"\b(the|your|you|their|cannot|must|may not|will|has|have|same)\b", re.I)),
    ("SpanishWords", re.compile(r"\b(el|la|los|las|debe|puede|tiene|obtiene|pierde|personaje)\b|[¿¡]", re.I)),
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

DYNAMIC_MARKERS = [
    ("SelectCString", re.compile(r"Select_CString", re.I)),
    ("ESHelper", re.compile(r"ES_(?:OA|XA|EA|ElLa|DelDela|AlAla|A|O)", re.I)),
    ("ScriptValue", re.compile(r"ScriptValue|GetScriptValue|GetValue|\|V[0-9]?|\|=\+?0|[0-9]+\s*%", re.I)),
    ("Concept", re.compile(r"\[[A-Za-z0-9_]+\||Concept\(|game_concept", re.I)),
    ("ScopeGetter", re.compile(r"ROOT\.|FROM\.|SCOPE\.|TARGET\.|CHARACTER\.|Get[A-Za-z0-9_]+\(", re.I)),
    ("CustomLoc", re.compile(r"Custom\(|custom_loc", re.I)),
    ("DynamicToken", re.compile(r"\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_]+", re.I)),
]

DOMAIN_MARKERS = [
    ("AccoladeTrait", re.compile(r"trait|GetTrait|accolade|knight|aptitude|prowess", re.I)),
    ("HolySiteReligion", re.compile(r"faith|religion|holy_site|holy site|church|temple|doctrine|piety", re.I)),
    ("CharacterInteraction", re.compile(r"interaction|scheme|duel|challenge|petition|character", re.I)),
    ("ActorTarget", re.compile(r"actor|target|recipient|owner|liege|vassal|host|guest", re.I)),
    ("CultureName", re.compile(r"culture|dynasty|nickname|house|name|GetName|GetDynasty", re.I)),
    ("TitleLaw", re.compile(r"title|county|duchy|kingdom|empire|law|succession|government|realm", re.I)),
    ("ArtifactActivity", re.compile(r"artifact|activity|tournament|hunt|feast|travel|legend|relic|inventory", re.I)),
    ("BuildingModifier", re.compile(r"building|holding|modifier|county_modifier|duchy_building", re.I)),
    ("DomainContext", re.compile(r"domain|realm|court|council|government", re.I)),
    ("EventContext", re.compile(r"events/|event|\.desc|desc_|option|flavor|story|scheme", re.I)),
]

GUARD_MARKERS = [
    ("DomainGuard", re.compile(r"culture|faith|religion|realm|title|law|government|succession|church|court", re.I)),
    ("CharacterGuard", re.compile(r"character|trait|GetTrait|accolade|knight|prowess|skill|opinion", re.I)),
    ("EventGuard", re.compile(r"event|events|\.desc|desc_|option|flavor|story|scheme|interaction", re.I)),
    ("RequirementEffectGuard", re.compile(r"requirement|requires|effect|gain|loss|modifier|tooltip", re.I)),
]

SECONDARY_MARKERS = [
    ("GenderLocalPlayer", re.compile(r"Select_CString|ES_(?:OA|XA|EA|ElLa|DelDela|AlAla|A|O)|local_player|GetPlayer|\bseu\b|\bsua\b", re.I)),
    ("ScriptValue", re.compile(r"ScriptValue|GetScriptValue|GetValue|\|V[0-9]?|\|=\+?0|[0-9]+\s*%", re.I)),
    ("Concept", re.compile(r"\[[A-Za-z0-9_]+\||Concept\(|game_concept", re.I)),
    ("TraitAccolade", re.compile(r"trait|GetTrait|accolade|knight|aptitude|prowess", re.I)),
    ("ArtifactActivity", re.compile(r"artifact|activity|tournament|hunt|feast|travel|legend|relic|inventory", re.I)),
    ("BuildingModifier", re.compile(r"building|holding|modifier|county_modifier|duchy_building", re.I)),
    ("EventContext", re.compile(r"events/|event|\.desc|desc_|option|flavor|story|scheme", re.I)),
    ("EffectList", re.compile(r"\n|\\n|EFFECT_LIST_BULLET|BULLET_WITH_TAB", re.I)),
    ("RequirementTooltip", re.compile(r"requirement|requires|tooltip|_tt\b|#T|#help", re.I)),
    ("DynamicParserEscape", re.compile(r"Custom\(|Select_CString|Concept\(|ScriptValue|GetTrait|ROOT\.|FROM\.|SCOPE\.|TARGET\.|\[[^\]]+\]|\$[^$]+\$", re.I)),
]

TOKEN_RE = re.compile(r"(\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_]+|[A-Z_]+\.[A-Za-z0-9_]+|[A-Za-z_]+\(.*?\)|\|[A-Za-z0-9_=+.-]+)")
BAD_ENCODING_RE = re.compile(r"Ã|Â|�|â€™|â€œ|â€|Ãƒ|Ã‚")
MOJIBAKE_REPLACEMENTS = {
    "Ã¡": "á",
    "Ã ": "à",
    "Ã¢": "â",
    "Ã£": "ã",
    "Ã©": "é",
    "Ãª": "ê",
    "Ã­": "í",
    "Ã³": "ó",
    "Ã´": "ô",
    "Ãµ": "õ",
    "Ãº": "ú",
    "Ã§": "ç",
    "Ã": "Á",
    "Ã€": "À",
    "Ã‚": "Â",
    "Ãƒ": "Ã",
    "Ã‰": "É",
    "ÃŠ": "Ê",
    "Ã“": "Ó",
    "Ã”": "Ô",
    "Ã•": "Õ",
    "Ãš": "Ú",
    "Ã‡": "Ç",
    "Âº": "º",
    "Âª": "ª",
    "Â«": "\"",
    "Â»": "\"",
    "â€™": "'",
    "â€œ": "\"",
    "â€": "\"",
}


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_requirement_effect_residual_review"
    spec = reports_dir / f"{stamp}_requirement_effect_residual_spec.json"
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


def tokens(text: str) -> list[str]:
    return TOKEN_RE.findall(text or "")


def mechanical_mojibake_fix(text: str) -> str:
    fixed = text
    for bad, good in MOJIBAKE_REPLACEMENTS.items():
        fixed = fixed.replace(bad, good)
    fixed = re.sub(r"\s+([,.!?;:])", r"\1", fixed)
    return fixed


def safe_repair_candidate(
    *,
    state: dict[str, Any],
    confirmed_text: str,
    output_text: str,
    residual_markers: list[str],
    dynamic_markers: list[str],
    domain_markers: list[str],
    secondary_markers: list[str],
) -> tuple[str, str, str] | None:
    if state["state_group"] != "pending" or int(state["is_closed"]) != 0:
        return None
    if int(state["needs_output_apply"]) != 0 or int(state["confirmed_matches_output"]) != 1:
        return None
    if confirmed_text != output_text:
        return None
    if "VisibleEncoding" not in residual_markers and "PunctuationSpacing" not in residual_markers:
        return None
    hard_dependencies = set(dynamic_markers) | set(domain_markers) | set(secondary_markers)
    hard_dependencies -= {"DynamicToken"}
    if hard_dependencies:
        return None
    corrected = mechanical_mojibake_fix(confirmed_text)
    if corrected == confirmed_text:
        return None
    if tokens(corrected) != tokens(confirmed_text):
        return None
    if BAD_ENCODING_RE.search(corrected):
        return None
    return "residual_safe_ptbr_fluency_repair", corrected, "short mechanical mojibake/spacing repair with CK3 tokens preserved"


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


def reuse_decision(policy: str, decision: str, registered: set[str], specs: set[str], rationale: str) -> tuple[str, str, str, str, str] | None:
    if policy in registered or policy in specs:
        return decision, policy if policy in registered else "", policy if policy in specs else "", policy, rationale
    return None


def classify(
    *,
    state: dict[str, Any],
    registered: set[str],
    specs: set[str],
    confirmed_text: str,
    output_text: str,
    residual_markers: list[str],
    language_residual_markers: list[str],
    requirement_markers: list[str],
    effect_markers: list[str],
    dynamic_markers: list[str],
    domain_markers: list[str],
    guard_markers: list[str],
    secondary_markers: list[str],
) -> tuple[str, str, str, str, bool, str, str]:
    if state["state_group"] != "pending" or int(state["is_closed"]) != 0:
        return "residual_blocked_uncertain", "", "", "state_guard", False, "", "segment is not pending in selected state run"
    if int(state["needs_output_apply"]) != 0 or int(state["confirmed_matches_output"]) != 1:
        return "residual_blocked_uncertain", "", "", "state_guard", False, "", "state guard failed: needs_output_apply or confirmed_matches_output"

    safe = safe_repair_candidate(
        state=state,
        confirmed_text=confirmed_text,
        output_text=output_text,
        residual_markers=residual_markers,
        dynamic_markers=dynamic_markers,
        domain_markers=domain_markers,
        secondary_markers=secondary_markers,
    )
    if safe is not None:
        decision, corrected_text, rationale = safe
        return decision, "", "", "protected_residual_apply_prompt", True, corrected_text, rationale

    secondary = set(secondary_markers)
    domains = set(domain_markers)
    dynamics = set(dynamic_markers)
    effects = set(effect_markers)
    requirements = set(requirement_markers)
    guards = set(guard_markers)

    for policy, decision, condition, rationale in [
        ("effect_list_gender_local_player_policy", "residual_reuse_effect_list_gender_local_player_policy", "GenderLocalPlayer" in secondary or "SelectCString" in dynamics or "ESHelper" in dynamics, "gender/local-player residual is better routed to registered effect-list policy"),
        ("effect_list_script_value_policy", "residual_reuse_effect_list_script_value_policy", "ScriptValue" in secondary or "ScriptValue" in dynamics, "ScriptValue residual is better routed to registered effect-list policy"),
        ("effect_list_concept_policy", "residual_reuse_effect_list_concept_policy", "Concept" in secondary or "Concept" in dynamics, "concept residual is better routed to registered effect-list policy"),
        ("effect_list_trait_accolade_policy", "residual_reuse_effect_list_trait_accolade_policy", "TraitAccolade" in secondary or "AccoladeTrait" in domains, "trait/accolade residual is better routed to registered effect-list policy"),
        ("artifact_activity_effect_policy", "residual_reuse_artifact_activity_effect_policy", "ArtifactActivity" in secondary or "ArtifactActivity" in domains, "artifact/activity residual is better routed to registered splitter"),
        ("building_modifier_effect_policy", "residual_reuse_building_modifier_effect_policy", "BuildingModifier" in secondary or "BuildingModifier" in domains, "building/modifier residual is better routed to registered splitter"),
        ("event_context_after_requirement_effect", "residual_reuse_event_context_policy", "EventContext" in secondary or "EventContext" in domains or "EventGuard" in guards, "event-context residual is better routed to registered event-context splitter"),
    ]:
        if condition:
            reused = reuse_decision(policy, decision, registered, specs, rationale)
            if reused is not None:
                decision_value, matched_registered, matched_spec, next_component, reason = reused
                return decision_value, matched_registered, matched_spec, next_component, False, "", reason

    if "AccoladeTrait" in domains or "TraitAccolade" in secondary:
        return "needs_residual_accolade_trait_policy", "", "", "accolade_trait_requirement_policy", False, "", "accolade/trait dependency remains"
    if "ScriptValue" in dynamics or "ScriptValue" in secondary:
        return "needs_residual_script_value_policy", "", "", "script_value_effect_policy", False, "", "ScriptValue dependency remains"
    if "HolySiteReligion" in domains:
        return "needs_residual_holy_site_policy", "", "", "holy_site_effect_name_policy", False, "", "holy-site/religion dependency remains"
    if "CharacterInteraction" in domains:
        return "needs_residual_character_interaction_policy", "", "", "event_context_character_interaction_policy", False, "", "character interaction dependency remains"
    if "ActorTarget" in domains or "ScopeGetter" in dynamics:
        return "needs_residual_actor_target_policy", "", "", "actor_target_requirement_effect_policy", False, "", "actor/target/scope dependency remains"
    if "CultureName" in domains:
        return "needs_residual_culture_or_name_policy", "", "", "culture_name_requirement_effect_policy", False, "", "culture/name dependency remains"
    if "TitleLaw" in domains:
        return "needs_residual_title_law_policy", "", "", "title_law_requirement_effect_policy", False, "", "title/law dependency remains"
    if "GenderLocalPlayer" in secondary or "SelectCString" in dynamics or "ESHelper" in dynamics:
        return "needs_residual_gender_local_player_policy", "", "", "gender_local_player_requirement_effect_policy", False, "", "gender/local-player dependency remains"
    if "ArtifactActivity" in domains:
        return "needs_residual_artifact_activity_policy", "", "", "artifact_activity_effect_policy", False, "", "artifact/activity dependency remains"
    if "BuildingModifier" in domains:
        return "needs_residual_building_modifier_policy", "", "", "building_modifier_effect_policy", False, "", "building/modifier dependency remains"
    if "RequirementTooltip" in secondary or requirements:
        return "needs_residual_requirement_tooltip_policy", "", "", "requirement_tooltip_policy", False, "", "requirement tooltip dependency remains"
    if "EffectList" in secondary or "EffectList" in effects:
        return "needs_residual_effect_list_policy", "", "", "effect_list_multiline_policy", False, "", "effect-list dependency remains"
    if "Concept" in dynamics or "Concept" in secondary:
        return "needs_residual_concept_policy", "", "", "concept_requirement_policy", False, "", "concept dependency remains"
    if "DomainGuard" in guards or "DomainContext" in domains:
        return "needs_residual_domain_context", "", "", "domain_context_after_requirement_effect", False, "", "domain context remains"
    if "EventGuard" in guards or "EventContext" in domains:
        return "needs_residual_event_context", "", "", "event_context_after_requirement_effect", False, "", "event context remains"
    if "DynamicParserEscape" in secondary or dynamics:
        return "needs_residual_dynamic_parser_escape", "", "", "ck3_dynamic_symbolic_parser", False, "", "dynamic parser escape remains"
    if residual_markers or language_residual_markers:
        return "residual_terminal_policy_with_guard", "", "", "residual_terminal_policy", False, "", "residual marker exists but not safe enough for apply"
    return "residual_blocked_uncertain", "", "", "human_review_or_evidence_collection", False, "", "insufficient residual evidence"


def count_records(values: Counter[str], field: str) -> list[dict[str, Any]]:
    return [{"record_type": field, "value": key, "segments": count} for key, count in values.most_common()]


def build_spec(
    *,
    decision_counts: Counter[str],
    reused_policies: Counter[str],
    reused_specs: Counter[str],
    residual_type_counts: Counter[str],
    next_components: list[str],
    safe_repair_count: int,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "created_for": "read_only_subpolicy_design",
        "parent_policy": "requirement_effect_router_readonly",
        "policy_id": "residual_repair_after_requirement_effect",
        "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
        "ledger_run_id": EXPECTED_LEDGER_RUN_ID,
        "entry_conditions": [
            "route == residual_repair_after_requirement_effect",
            "segment remains pending",
            "needs_output_apply == 0",
            "confirmed_matches_output == 1",
        ],
        "safe_repair_conditions": [
            "confirmed_text == output_text",
            "CK3 tokens preserved exactly",
            "short mechanical repair only",
            "no context/domain/parser dependency",
            "requires separate protected apply prompt",
        ],
        "reused_registered_policies": [{"agent_key": key, "sampled": count} for key, count in reused_policies.most_common()],
        "reused_catalog_specs": [{"policy_id": key, "sampled": count} for key, count in reused_specs.most_common()],
        "residual_types": [{"type": key, "sampled": count} for key, count in residual_type_counts.most_common()],
        "resolution_order": [
            "state guard",
            "safe mechanical repair guard",
            "reuse registered/cataloged policies",
            "route to focused residual subpolicies",
            "terminal guarded residual",
            "blocked uncertain",
        ],
        "next_components": next_components,
        "blocked_conditions": [
            "state guard failed",
            "tokens would change",
            "context/domain/parser dependency",
            "insufficient residual evidence",
        ],
        "safe_repair_candidates": safe_repair_count,
        "promotion_gate": "read_only_review_only_apply_requires_separate_protected_prompt",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only residual after requirement/effect review.")
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
    residual_counts: Counter[str] = Counter()
    language_counts: Counter[str] = Counter()
    requirement_counts: Counter[str] = Counter()
    effect_counts: Counter[str] = Counter()
    dynamic_counts: Counter[str] = Counter()
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
        residual_markers = detect(RESIDUAL_MARKERS, blob)
        language_residual_markers = detect(LANGUAGE_RESIDUAL_MARKERS, blob)
        requirement_markers = detect(REQUIREMENT_MARKERS, blob)
        effect_markers = detect(EFFECT_MARKERS, blob)
        dynamic_markers = detect(DYNAMIC_MARKERS, blob)
        domain_markers = detect(DOMAIN_MARKERS, blob)
        guard_markers = detect(GUARD_MARKERS, blob)
        secondary_markers = detect(SECONDARY_MARKERS, blob)
        decision, matched_registered_policy, matched_catalog_spec, next_component, requires_apply, corrected_text, rationale = classify(
            state=record["state"],
            registered=registered,
            specs=specs,
            confirmed_text=text["confirmed_text"],
            output_text=text["output_text"],
            residual_markers=residual_markers,
            language_residual_markers=language_residual_markers,
            requirement_markers=requirement_markers,
            effect_markers=effect_markers,
            dynamic_markers=dynamic_markers,
            domain_markers=domain_markers,
            guard_markers=guard_markers,
            secondary_markers=secondary_markers,
        )
        family_counts.update(record["families_open"])
        residual_counts.update(residual_markers)
        language_counts.update(language_residual_markers)
        requirement_counts.update(requirement_markers)
        effect_counts.update(effect_markers)
        dynamic_counts.update(dynamic_markers)
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
                "residual_markers": residual_markers,
                "language_residual_markers": language_residual_markers,
                "requirement_markers": requirement_markers,
                "effect_markers": effect_markers,
                "dynamic_markers": dynamic_markers,
                "domain_markers": domain_markers,
                "matched_registered_policy": matched_registered_policy,
                "matched_catalog_spec": matched_catalog_spec,
                "guard_markers": guard_markers,
                "secondary_markers": secondary_markers,
                "residual_decision": decision,
                "next_component": next_component,
                "requires_lifecycle_later": False,
                "requires_apply_later": requires_apply,
                "corrected_text": corrected_text,
                "rationale": rationale,
            }
        )

    reuse_total = sum(1 for row in results if row["matched_registered_policy"] or row["matched_catalog_spec"])
    safe_repair_total = sum(1 for row in results if str(row["residual_decision"]).startswith("residual_safe_"))
    terminal_total = decision_counts["residual_terminal_policy_with_guard"]
    dominant_decision, dominant_count = decision_counts.most_common(1)[0] if decision_counts else ("", 0)
    if safe_repair_total >= 30:
        next_prompt = "chat_exec_requirement_effect_residual_protected_apply_prompt.md"
        recommendation = "protected_apply_prompt_later"
    elif dominant_decision.startswith("needs_") and dominant_count >= 50:
        next_prompt = f"chat_exec_{dominant_decision.removeprefix('needs_')}_review_prompt.md"
        recommendation = "open_narrow_subpolicy"
    elif reuse_total >= 70:
        next_prompt = "chat_exec_requirement_effect_residual_policy_catalog_registration_prompt.md"
        recommendation = "register_readonly_reuse_splitter"
    elif terminal_total >= 70:
        next_prompt = "chat_exec_requirement_effect_residual_terminal_spec_registration_prompt.md"
        recommendation = "register_terminal_readonly"
    else:
        next_prompt = "chat_exec_accolade_trait_requirement_policy_review_prompt.md"
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
        "safe_repair_candidates_future": safe_repair_total,
        "terminal_policy_count": terminal_total,
        "dominant_subtype": dominant_decision,
        "dominant_count": dominant_count,
        "ready_lifecycle_future": 0,
        "apply_candidates_future": safe_repair_total,
        "requires_lifecycle_later": False,
        "requires_apply_later_count": safe_repair_total,
        "recommendation": recommendation,
        "next_prompt": next_prompt,
        "policy_shape": "protected_apply_queue" if safe_repair_total >= 30 else ("reuse_splitter_route" if reuse_total >= 70 else "residual_splitter_candidate"),
    }
    spec = build_spec(
        decision_counts=decision_counts,
        reused_policies=reused_policy_counts,
        reused_specs=reused_spec_counts,
        residual_type_counts=residual_counts + language_counts + domain_counts + dynamic_counts,
        next_components=[next_prompt],
        safe_repair_count=safe_repair_total,
    )

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(summary, ensure_ascii=False, sort_keys=True) + "\n")
        for decision, count in decision_counts.most_common():
            handle.write(json.dumps({"record_type": "decision_count", "residual_decision": decision, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for section_name, counts in [
            ("top_family", family_counts),
            ("top_residual_marker", residual_counts),
            ("top_language_residual_marker", language_counts),
            ("top_requirement_marker", requirement_counts),
            ("top_effect_marker", effect_counts),
            ("top_dynamic_marker", dynamic_counts),
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
        handle.write("Requirement/effect residual review\n\n")
        handle.write(f"- universo estimado: {universe}\n")
        handle.write(f"- total revisado: {len(results)}\n")
        handle.write(f"- reuso policies/specs catalogadas: {reuse_total}\n")
        handle.write(f"- safe repair candidates futuros: {safe_repair_total}\n")
        handle.write(f"- terminal policies futuras: {terminal_total}\n")
        handle.write("- ready lifecycle futuro: 0\n")
        handle.write(f"- apply candidates futuro: {safe_repair_total}\n")
        handle.write(f"- dominante: {dominant_decision} ({dominant_count})\n")
        handle.write(f"- recomendacao: {recommendation}\n")
        handle.write(f"- proximo prompt: {next_prompt}\n\n")
        handle.write("Decisoes\n")
        for decision, count in decision_counts.most_common():
            handle.write(f"- {decision}: {count}\n")
        for title, counts in [
            ("Top families_open", family_counts),
            ("Top residual markers", residual_counts),
            ("Top language residual markers", language_counts),
            ("Top requirement markers", requirement_counts),
            ("Top effect markers", effect_counts),
            ("Top dynamic markers", dynamic_counts),
            ("Top domain markers", domain_counts),
            ("Top guard markers", guard_counts),
            ("Top secondary markers", secondary_counts),
        ]:
            handle.write(f"\n{title}\n")
            for key, count in counts.most_common(15):
                handle.write(f"- {key}: {count}\n")
        handle.write("\nRespostas\n")
        handle.write("- residual_repair_after_requirement_effect contem poucos reparos seguros diretos nesta amostra; a rota e majoritariamente roteamento incompleto se reuso/subpolicy dominar.\n")
        handle.write("- componente read-only real: sim se reuso >= 70; caso contrario manter como fila residual/splitter candidato.\n")
        handle.write("- apply protegido em curto prazo: somente em prompt separado e apenas para corrected_text validado.\n")
        handle.write(f"- proximo prompt recomendado: {next_prompt}\n")
        handle.write("- sem escrita em banco, source ou output.\n")

    print(f"txt: {txt_path}")
    print(f"jsonl: {jsonl_path}")
    print(f"spec: {spec_path}")
    print(f"universe_estimated: {universe}")
    print(f"total_reviewed: {len(results)}")
    print(f"reused_cataloged_policy_count: {reuse_total}")
    print(f"safe_repair_candidates_future: {safe_repair_total}")
    print(f"terminal_policy_count: {terminal_total}")
    print(f"dominant_subtype: {dominant_decision}")
    print(f"dominant_count: {dominant_count}")
    print(f"next_prompt: {next_prompt}")


if __name__ == "__main__":
    main()
