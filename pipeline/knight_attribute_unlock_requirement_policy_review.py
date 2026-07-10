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
SOURCE_DECISION = "needs_knight_attribute_unlock_requirement_policy"

ALLOWED_DECISIONS = {
    "unlock_requirement_ready_false_reopen",
    "unlock_requirement_ready_lifecycle",
    "needs_unlock_trait_requirement_policy",
    "needs_unlock_acclaimed_knight_entity_policy",
    "needs_unlock_trait_list_policy",
    "needs_unlock_aptitude_rank_policy",
    "needs_unlock_maa_or_culture_policy",
    "needs_unlock_title_law_policy",
    "needs_unlock_actor_target_policy",
    "needs_unlock_script_value_policy",
    "needs_unlock_effect_list_policy",
    "needs_unlock_domain_context",
    "needs_unlock_event_context",
    "needs_unlock_residual_repair",
    "needs_unlock_dynamic_parser_after_policy",
    "unlock_requirement_blocked_uncertain",
}

UNLOCK_MARKERS: list[tuple[str, re.Pattern[str]]] = [
    ("UnlockKey", re.compile(r"\bunlock\b|UNLOCK_", re.I)),
    ("CanBecome", re.compile(r"pode se tornar|pode tornar-se|pode converter-se|pode ser tornado|pode ser reconhecido|pode criar", re.I)),
    ("AttributeUnlock", re.compile(r"_attribute|house_knight_attribute|GetAccoladeType\([^)]*attribute", re.I)),
]

TRAIT_MARKERS: list[tuple[str, re.Pattern[str]]] = [
    ("GetTrait", re.compile(r"GetTrait\(", re.I)),
    ("Trait", re.compile(r"\btrait\b|traits|trait_level_track|lifestyle_", re.I)),
    ("MultipleTrait", re.compile(r"GetTrait\([^)]*\).+GetTrait\(", re.I | re.S)),
    ("GetAccoladeType", re.compile(r"GetAccoladeType\(", re.I)),
]

ACCOLADE_MARKERS: list[tuple[str, re.Pattern[str]]] = [
    ("Accolade", re.compile(r"accolade|Accolade|accolade_type", re.I)),
    ("AcclaimedKnight", re.compile(r"acclaimed|acclaimed_knight", re.I)),
    ("Knight", re.compile(r"knight|cavaleir|cavalaria|prowess", re.I)),
    ("KnightAttribute", re.compile(r"attribute|aptitude|house_knight|besieger|charmer|disciplinarian|huntsmaster|idealist|lancer|mentor|stalwart|tactician|valiant|marauder|politicker|reeve|manipulator|outrider|scoundrel|thug", re.I)),
]

REQUIREMENT_MARKERS: list[tuple[str, re.Pattern[str]]] = [
    ("Tooltip", re.compile(r"tooltip|_tt\b|#T\b", re.I)),
    ("Requirement", re.compile(r"requirement|required|trigger|valid|allowed|cannot|can_|requisito|requisitos|não atende", re.I)),
    ("Unlock", re.compile(r"unlock|desbloque|pode criar|pode se tornar|pode converter|pode ser tornado|tornar-se|converter-se", re.I)),
    ("Condition", re.compile(r"NO_CHANCE|invalid|valid|blocked|disabled|missing|has_|is_|not_", re.I)),
]

SECONDARY_MARKERS: list[tuple[str, re.Pattern[str]]] = [
    ("CultureMaA", re.compile(r"men_at_arms|maa|army|culture|tradition|heritage|innovation|knight_culture", re.I)),
    ("TitleLaw", re.compile(r"title|law|government|realm|succession|county|duchy|kingdom|empire|vassal|liege|rank|holding", re.I)),
    ("ActorTarget", re.compile(r"\bactor\b|\btarget\b|\brecipient\b|\baddressee\b|ROOT\.|FROM\.|TARGET\.|Character\.", re.I)),
    ("ScriptValue", re.compile(r"ScriptValue|GetScriptValue|\|V[0-9]?|\|=\+?0|[0-9]+%", re.I)),
    ("EffectList", re.compile(r"\\n|\n|\$EFFECT_LIST_BULLET\$|#indent|#weak|#bold|#high|#low|#P|#N", re.I)),
    ("AptitudeRank", re.compile(r"aptitude|quality|rank|level|attribute level|avalia", re.I)),
    ("Concept", re.compile(r"\[[A-Za-z0-9_]+\|[^\]]+\]|\[Concept\(|Concept\(", re.I)),
    ("Domain", re.compile(r"culture|religion|faith|doctrine|tradition|domain", re.I)),
    ("Event", re.compile(r"event|\.desc|desc\.|option|toast|dialogue|story|scheme|interaction|memory|battle", re.I)),
    ("ResidualVisible", re.compile(r"NÃƒ|ÃƒÆ’|Ã‚|ï¿½|\b(?:the|your|you|their|cannot|consiguio|consiguiÃ³|sentisteis|sintieron|sera|serÃ¡|mas|mÃ¡s|facil|fÃ¡cil)\b", re.I)),
    ("DynamicToken", re.compile(r"Custom\(|Select_CString|Concept\(|ScriptValue|GetTrait|GetAccoladeType|ROOT\.|FROM\.|SCOPE\.|TARGET\.|\[[^\]]+\]|\$[^$]+\$", re.I)),
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
        and row.get("knight_attribute_decision") == SOURCE_DECISION
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
    state: dict[str, Any] | None,
    unlock_markers: list[str],
    trait_markers: list[str],
    accolade_markers: list[str],
    requirement_markers: list[str],
    secondary_markers: list[str],
) -> tuple[str, str, bool, str]:
    unlock = set(unlock_markers)
    trait = set(trait_markers)
    accolade = set(accolade_markers)
    req = set(requirement_markers)
    secondary = set(secondary_markers)
    if not state or state["state_group"] != "pending" or int(state["is_closed"] or 0) != 0:
        return "unlock_requirement_blocked_uncertain", "human_review_or_evidence_collection", False, "segment is not pending in selected state run"
    if int(state["needs_output_apply"] or 0) != 0 or int(state["confirmed_matches_output"] or 0) != 1:
        return "unlock_requirement_blocked_uncertain", "state_guard", False, "state guard failed: needs_output_apply or confirmed_matches_output"
    if "ResidualVisible" in secondary:
        return "needs_unlock_residual_repair", "residual_dependency_filtered_repair", False, "visible residual/mojibake remains"
    if "ScriptValue" in secondary:
        return "needs_unlock_script_value_policy", "unlock_script_value_policy", False, "ScriptValue/numeric dependency is mixed with unlock requirement"
    if "EffectList" in secondary:
        return "needs_unlock_effect_list_policy", "unlock_effect_list_policy", False, "effect-list/multiline dependency is mixed with unlock requirement"
    if "CultureMaA" in secondary:
        return "needs_unlock_maa_or_culture_policy", "unlock_maa_or_culture_policy", False, "MaA/culture condition is mixed with unlock requirement"
    if "TitleLaw" in secondary:
        return "needs_unlock_title_law_policy", "unlock_title_law_policy", False, "title/law context is mixed with unlock requirement"
    if "ActorTarget" in secondary:
        return "needs_unlock_actor_target_policy", "unlock_actor_target_policy", False, "actor/target scope is mixed with unlock requirement"
    if "MultipleTrait" in trait:
        return "needs_unlock_trait_list_policy", "unlock_trait_list_policy", False, "multiple trait references need trait-list handling"
    if "AptitudeRank" in secondary:
        return "needs_unlock_aptitude_rank_policy", "unlock_aptitude_rank_policy", False, "aptitude/rank/quality marker is present"
    if "AcclaimedKnight" in accolade:
        return "needs_unlock_acclaimed_knight_entity_policy", "unlock_acclaimed_knight_entity_policy", False, "unlock condition creates or recognizes an acclaimed knight entity"
    if {"GetTrait", "Trait"} & trait or {"UnlockKey", "CanBecome", "AttributeUnlock"} & unlock:
        return "needs_unlock_trait_requirement_policy", "unlock_trait_requirement_policy", False, "plain unlock/trait requirement remains after entity-specific checks"
    if "Domain" in secondary or "Concept" in secondary:
        return "needs_unlock_domain_context", "domain_context_composer", False, "domain/concept context remains after unlock routing"
    if "Event" in secondary:
        return "needs_unlock_event_context", "event_context_composer", False, "event context remains after unlock routing"
    if "DynamicToken" in secondary:
        return "needs_unlock_dynamic_parser_after_policy", "ck3_dynamic_symbolic_parser", False, "dynamic token remains after unlock routing"
    if req and int(state["confirmed_matches_output"] or 0) == 1:
        if int(state["needs_reopen"] or 0) == 1:
            return "unlock_requirement_ready_false_reopen", "false_reopen_lifecycle_bridge", True, "plain unlock requirement may be a future false-reopen candidate"
        return "unlock_requirement_ready_lifecycle", "unlock_requirement_lifecycle_bridge", True, "plain unlock requirement may be a future lifecycle candidate"
    return "unlock_requirement_blocked_uncertain", "human_review_or_evidence_collection", False, "insufficient unlock marker evidence"


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_knight_attribute_unlock_requirement_policy_review"
    spec = reports_dir / f"{stamp}_knight_attribute_unlock_requirement_policy_spec.json"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), spec


def build_spec(run_id: int, decisions: Counter[str], unlock_counts: Counter[str], secondary_counts: Counter[str]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "created_for": "read_only_subpolicy_design",
        "parent_policy": "accolade_knight_attribute_policy",
        "policy_id": "knight_attribute_unlock_requirement_policy",
        "segment_state_run_id": run_id,
        "ledger_run_id": LEDGER_RUN_ID,
        "entry_conditions": [
            "knight_attribute_decision == needs_knight_attribute_unlock_requirement_policy",
            "segment remains pending",
            "needs_output_apply == 0",
            "confirmed_matches_output == 1",
        ],
        "unlock_requirement_types": [{"type": marker, "sampled": count} for marker, count in unlock_counts.most_common()],
        "resolution_order": [
            "state guard",
            "residual visible guard",
            "script value and effect-list blockers",
            "MaA/culture, title/law, actor/target blockers",
            "trait list and aptitude/rank",
            "acclaimed knight entity",
            "plain trait/unlock requirement",
            "domain/event/dynamic handoff",
        ],
        "next_components": [
            "unlock_acclaimed_knight_entity_policy",
            "unlock_trait_requirement_policy",
            "unlock_trait_list_policy",
            "unlock_aptitude_rank_policy",
            "unlock_maa_or_culture_policy",
            "unlock_title_law_policy",
            "unlock_script_value_policy",
            "unlock_effect_list_policy",
            "domain_context_composer",
            "event_context_composer",
            "residual_dependency_filtered_repair",
            "ck3_dynamic_symbolic_parser",
        ],
        "blocked_conditions": [
            "state guard failed",
            "visible residual/mojibake",
            "mixed unlock/acclaimed/concept marker stack",
            "ambiguous CK3 dynamic structure",
        ],
        "promotion_gate": "Keep read-only until the dominant acclaimed-knight unlock subtype is reviewed separately; no lifecycle/apply in this broad subpolicy.",
        "observed_decision_counts": dict(decisions),
        "observed_secondary_marker_counts": dict(secondary_counts),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only knight attribute unlock requirement policy review.")
    parser.add_argument("--knight-attribute-jsonl", required=True, type=Path)
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    args = parser.parse_args()

    rows = source_rows(args.knight_attribute_jsonl)
    conn = connect_readonly()
    states = fetch_states(conn, args.segment_state_run_id, [int(row["segment_id"]) for row in rows])

    results: list[dict[str, Any]] = []
    decision_counts: Counter[str] = Counter()
    unlock_counts: Counter[str] = Counter()
    trait_counts: Counter[str] = Counter()
    accolade_counts: Counter[str] = Counter()
    req_counts: Counter[str] = Counter()
    secondary_counts: Counter[str] = Counter()
    lifecycle_later = 0
    apply_later = 0
    pending_count = 0

    for row in rows:
        segment_id = int(row["segment_id"])
        state = states.get(segment_id)
        if state and state["state_group"] == "pending" and int(state["is_closed"] or 0) == 0:
            pending_count += 1
        blob = " ".join(str(row.get(key) or "") for key in ("relative_path", "source_key", "old_text", "confirmed_text", "output_text"))
        unlock_markers = markers(UNLOCK_MARKERS, blob)
        trait_markers = markers(TRAIT_MARKERS, blob)
        accolade_markers = markers(ACCOLADE_MARKERS, blob)
        requirement_markers = markers(REQUIREMENT_MARKERS, blob)
        secondary_markers = markers(SECONDARY_MARKERS, blob)
        decision, component, lifecycle, rationale = classify(state, unlock_markers, trait_markers, accolade_markers, requirement_markers, secondary_markers)
        if decision not in ALLOWED_DECISIONS:
            raise SystemExit(f"unknown decision {decision} for segment_id {segment_id}")
        requires_apply_later = False
        lifecycle_later += int(lifecycle)
        apply_later += int(requires_apply_later)
        decision_counts[decision] += 1
        unlock_counts.update(unlock_markers or ["NoUnlockMarker"])
        trait_counts.update(trait_markers or ["NoTraitMarker"])
        accolade_counts.update(accolade_markers or ["NoAccoladeMarker"])
        req_counts.update(requirement_markers or ["NoRequirementMarker"])
        secondary_counts.update(secondary_markers or ["NoSecondaryMarker"])
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
                "unlock_markers": unlock_markers,
                "trait_markers": trait_markers,
                "accolade_markers": accolade_markers,
                "requirement_markers": requirement_markers,
                "secondary_markers": secondary_markers,
                "unlock_requirement_decision": decision,
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
    if dominant == "needs_unlock_acclaimed_knight_entity_policy":
        next_prompt = "chat_exec_unlock_acclaimed_knight_entity_policy_review_prompt.md"
    elif dominant == "needs_unlock_trait_requirement_policy":
        next_prompt = "chat_exec_unlock_trait_requirement_policy_review_prompt.md"
    elif dominant == "needs_unlock_trait_list_policy":
        next_prompt = "chat_exec_unlock_trait_list_policy_review_prompt.md"
    else:
        next_prompt = "chat_exec_unlock_requirement_residual_or_context_review_prompt.md"

    txt_path, jsonl_path, spec_path = output_paths()
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps({
            "record_type": "summary",
            "source_decision": SOURCE_DECISION,
            "total_reviewed": len(results),
            "pending_count": pending_count,
            "decision_counts": dict(decision_counts),
            "unlock_marker_counts": dict(unlock_counts),
            "trait_marker_counts": dict(trait_counts),
            "accolade_marker_counts": dict(accolade_counts),
            "requirement_marker_counts": dict(req_counts),
            "secondary_marker_counts": dict(secondary_counts),
            "ready_lifecycle_future": lifecycle_later,
            "apply_candidates_future": apply_later,
            "dominant_subtype": dominant,
            "next_prompt": next_prompt,
        }, ensure_ascii=False, sort_keys=True) + "\n")
        for decision, count in decision_counts.most_common():
            handle.write(json.dumps({"record_type": "decision_count", "unlock_requirement_decision": decision, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for marker, count in unlock_counts.most_common():
            handle.write(json.dumps({"record_type": "unlock_marker_count", "marker": marker, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for marker, count in trait_counts.most_common():
            handle.write(json.dumps({"record_type": "trait_marker_count", "marker": marker, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for marker, count in accolade_counts.most_common():
            handle.write(json.dumps({"record_type": "accolade_marker_count", "marker": marker, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for marker, count in req_counts.most_common():
            handle.write(json.dumps({"record_type": "requirement_marker_count", "marker": marker, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for marker, count in secondary_counts.most_common():
            handle.write(json.dumps({"record_type": "secondary_marker_count", "marker": marker, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for priority, (prompt, rationale) in enumerate([
            (next_prompt, "dominant subtype from unlock requirement review"),
            ("chat_exec_knight_attribute_maa_culture_policy_review_prompt.md", "secondary sibling from knight attribute review"),
            ("chat_exec_requirement_effect_list_multiline_subpolicy_review_prompt.md", "fallback parent route if unlock remains fragmented"),
        ], 1):
            handle.write(json.dumps({"record_type": "strategy", "priority": priority, "next_prompt": prompt, "rationale": rationale}, ensure_ascii=False, sort_keys=True) + "\n")
        for result in results:
            handle.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")

    with spec_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(build_spec(args.segment_state_run_id, decision_counts, unlock_counts, secondary_counts), handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("Knight attribute unlock requirement policy review\n\n")
        handle.write(f"total_revisado: {len(results)}\n")
        handle.write(f"pending_count: {pending_count}\n")
        handle.write(f"ready_lifecycle_future: {lifecycle_later}\n")
        handle.write(f"apply_candidates_future: {apply_later}\n")
        handle.write(f"subtipo_dominante: {dominant}\n\n")
        handle.write("unlock_requirement_decision_counts:\n")
        for decision, count in decision_counts.most_common():
            handle.write(f"- {decision}: {count}\n")
        handle.write("\nTop unlock markers:\n")
        for marker, count in unlock_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nTop trait markers:\n")
        for marker, count in trait_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nTop accolade markers:\n")
        for marker, count in accolade_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nTop requirement markers:\n")
        for marker, count in req_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nTop secondary markers:\n")
        for marker, count in secondary_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nAnalise\n")
        handle.write("- knight_attribute_unlock_requirement_policy deve virar componente read-only real: sim, como splitter de unlock/acclaimed knight.\n")
        handle.write("- Lifecycle/apply curto: nao; zero candidatos nesta revisao.\n")
        handle.write(f"- Subtipo dominante: {dominant}.\n")
        handle.write(f"- Proximo prompt recomendado: {next_prompt}.\n")
        handle.write("- Confirma que as camadas requirement_tooltip/scope_getter/GetTrait/accolade/knight_attribute devem ficar antes do parser generico.\n")
        handle.write("\nProximos prompts recomendados\n")
        handle.write(f"1. {next_prompt}\n")
        handle.write("2. chat_exec_knight_attribute_maa_culture_policy_review_prompt.md\n")
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
    print("top_unlock_markers:")
    for marker, count in unlock_counts.most_common(10):
        print(f"  {marker}: {count}")


if __name__ == "__main__":
    main()
