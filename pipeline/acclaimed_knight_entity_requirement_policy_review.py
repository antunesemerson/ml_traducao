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
SOURCE_DECISION = "needs_acclaimed_knight_entity_requirement_policy"

ALLOWED_DECISIONS = {
    "entity_requirement_ready_false_reopen",
    "entity_requirement_ready_lifecycle",
    "needs_entity_requirement_trait_unlock_policy",
    "needs_entity_requirement_acclaimed_knight_unlock_policy",
    "needs_entity_requirement_rank_or_attribute_policy",
    "needs_entity_requirement_maa_or_culture_policy",
    "needs_entity_requirement_title_law_policy",
    "needs_entity_requirement_actor_target_policy",
    "needs_entity_requirement_activity_context_policy",
    "needs_entity_requirement_script_value_policy",
    "needs_entity_requirement_effect_list_policy",
    "needs_entity_requirement_domain_context",
    "needs_entity_requirement_event_context",
    "needs_entity_requirement_residual_repair",
    "needs_entity_requirement_dynamic_parser_after_policy",
    "entity_requirement_blocked_uncertain",
}

ENTITY_MARKERS: list[tuple[str, re.Pattern[str]]] = [
    ("AcclaimedKnight", re.compile(r"acclaimed|acclaimed_knight", re.I)),
    ("AccoladeKnight", re.compile(r"accolade_knight|Accolade\.|accolade knight", re.I)),
    ("Knight", re.compile(r"knight|cavaleir|cavalaria|prowess", re.I)),
    ("Attribute", re.compile(r"_attribute|attribute|GetAccoladeType\([^)]*attribute", re.I)),
]

UNLOCK_MARKERS: list[tuple[str, re.Pattern[str]]] = [
    ("AttributeUnlock", re.compile(r"_attribute|GetAccoladeType\([^)]*attribute", re.I)),
    ("CanBecome", re.compile(r"pode se tornar|pode tornar-se|pode converter-se|pode ser tornado|pode ser reconhecido", re.I)),
    ("UnlockKey", re.compile(r"\bunlock\b|UNLOCK_", re.I)),
]

REQUIREMENT_MARKERS: list[tuple[str, re.Pattern[str]]] = [
    ("Unlock", re.compile(r"unlock|desbloque|pode criar|pode se tornar|pode converter|pode ser tornado|tornar-se|converter-se", re.I)),
    ("Tooltip", re.compile(r"tooltip|_tt\b|#T\b", re.I)),
    ("Requirement", re.compile(r"requirement|required|trigger|valid|allowed|cannot|can_|requisito|requisitos|não atende", re.I)),
    ("Condition", re.compile(r"NO_CHANCE|invalid|valid|blocked|disabled|missing|has_|is_|not_", re.I)),
]

SECONDARY_MARKERS: list[tuple[str, re.Pattern[str]]] = [
    ("TraitList", re.compile(r"GetTrait\([^)]*\).+GetTrait\(", re.I | re.S)),
    ("CultureMaA", re.compile(r"men_at_arms|maa|army|culture|tradition|heritage|innovation|knight_culture", re.I)),
    ("TitleLaw", re.compile(r"title|law|government|realm|succession|county|duchy|kingdom|empire|vassal|liege|rank|holding", re.I)),
    ("ActorTarget", re.compile(r"\bactor\b|\btarget\b|\brecipient\b|\baddressee\b|ROOT\.|FROM\.|TARGET\.|Character\.", re.I)),
    ("LocalPlayer", re.compile(r"Select_CString|ES_(?:OA|XA|EA|ElLa|DelDela|AlAla|A|O)|Get(?:SheHe|HerHis|HerHim|WomanMan|WomenMen)|GetPlayer|GetLocalPlayer|local_player|\bvoc(?:ê|Ãª)\b|\bseu\b|\bsua\b", re.I)),
    ("Activity", re.compile(r"activity|tournament|travel|feast|hunt|pilgrimage|wedding|journey|battle", re.I)),
    ("ScriptValue", re.compile(r"ScriptValue|GetScriptValue|\|V[0-9]?|\|=\+?0|[0-9]+%", re.I)),
    ("EffectList", re.compile(r"\\n|\n|\$EFFECT_LIST_BULLET\$|#indent|#weak|#bold|#high|#low|#P|#N", re.I)),
    ("RankAttribute", re.compile(r"aptitude|quality|rank|level|attribute level|avalia", re.I)),
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
        and row.get("acclaimed_knight_entity_decision") == SOURCE_DECISION
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
    entity_markers: list[str],
    unlock_markers: list[str],
    requirement_markers: list[str],
    secondary_markers: list[str],
) -> tuple[str, str, bool, str]:
    entity = set(entity_markers)
    unlock = set(unlock_markers)
    secondary = set(secondary_markers)
    if not state or state["state_group"] != "pending" or int(state["is_closed"] or 0) != 0:
        return "entity_requirement_blocked_uncertain", "human_review_or_evidence_collection", False, "segment is not pending in selected state run"
    if int(state["needs_output_apply"] or 0) != 0 or int(state["confirmed_matches_output"] or 0) != 1:
        return "entity_requirement_blocked_uncertain", "state_guard", False, "state guard failed: needs_output_apply or confirmed_matches_output"
    if "ResidualVisible" in secondary:
        return "needs_entity_requirement_residual_repair", "residual_dependency_filtered_repair", False, "visible residual/mojibake remains"
    if "TraitList" in secondary:
        return "needs_entity_requirement_trait_unlock_policy", "entity_trait_unlock_policy", False, "trait-list/unlock dependency is present"
    if "CultureMaA" in secondary:
        return "needs_entity_requirement_maa_or_culture_policy", "entity_maa_or_culture_policy", False, "MaA/culture dependency is present"
    if "TitleLaw" in secondary:
        return "needs_entity_requirement_title_law_policy", "entity_title_law_policy", False, "title/law dependency is present"
    if "LocalPlayer" in secondary:
        return "needs_entity_requirement_actor_target_policy", "entity_actor_target_policy", False, "local-player/scope dependency is present"
    if "ActorTarget" in secondary:
        return "needs_entity_requirement_actor_target_policy", "entity_actor_target_policy", False, "actor/target dependency is present"
    if "Activity" in secondary:
        return "needs_entity_requirement_activity_context_policy", "entity_activity_context_policy", False, "activity context dependency is present"
    if "ScriptValue" in secondary:
        return "needs_entity_requirement_script_value_policy", "entity_script_value_policy", False, "ScriptValue/numeric dependency is present"
    if "EffectList" in secondary:
        return "needs_entity_requirement_effect_list_policy", "entity_effect_list_policy", False, "effect-list/multiline dependency is present"
    if "RankAttribute" in secondary:
        return "needs_entity_requirement_rank_or_attribute_policy", "entity_rank_or_attribute_policy", False, "rank/attribute evaluation dependency is present"
    if {"AcclaimedKnight", "AccoladeKnight", "Knight", "Attribute"} & entity and unlock:
        return "needs_entity_requirement_acclaimed_knight_unlock_policy", "entity_acclaimed_knight_unlock_policy", False, "direct Acclaimed Knight unlock requirement is the central surface"
    if "Domain" in secondary or "Concept" in secondary:
        return "needs_entity_requirement_domain_context", "domain_context_composer", False, "domain/concept context remains after entity routing"
    if "Event" in secondary:
        return "needs_entity_requirement_event_context", "event_context_composer", False, "event context remains after entity routing"
    if "DynamicToken" in secondary:
        return "needs_entity_requirement_dynamic_parser_after_policy", "ck3_dynamic_symbolic_parser", False, "dynamic token remains after entity routing"
    return "entity_requirement_blocked_uncertain", "human_review_or_evidence_collection", False, "insufficient entity requirement marker evidence"


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_acclaimed_knight_entity_requirement_policy_review"
    spec = reports_dir / f"{stamp}_acclaimed_knight_entity_requirement_policy_spec.json"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), spec


def build_spec(run_id: int, decisions: Counter[str], entity_counts: Counter[str], secondary_counts: Counter[str], branch_action: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "created_for": "read_only_subpolicy_design",
        "parent_policy": "unlock_acclaimed_knight_entity_policy",
        "policy_id": "acclaimed_knight_entity_requirement_policy",
        "segment_state_run_id": run_id,
        "ledger_run_id": LEDGER_RUN_ID,
        "entry_conditions": [
            "acclaimed_knight_entity_decision == needs_acclaimed_knight_entity_requirement_policy",
            "segment remains pending",
            "needs_output_apply == 0",
            "confirmed_matches_output == 1",
        ],
        "entity_requirement_types": [{"type": marker, "sampled": count} for marker, count in entity_counts.most_common()],
        "resolution_order": [
            "state guard",
            "residual visible guard",
            "remaining dependency guards",
            "direct Acclaimed Knight unlock requirement",
            "domain/event/dynamic handoff",
        ],
        "next_components": [
            "entity_acclaimed_knight_unlock_policy",
            "entity_trait_unlock_policy",
            "entity_maa_or_culture_policy",
            "entity_title_law_policy",
            "entity_actor_target_policy",
            "entity_activity_context_policy",
            "domain_context_composer",
            "event_context_composer",
            "ck3_dynamic_symbolic_parser",
        ],
        "blocked_conditions": [
            "state guard failed",
            "visible residual/mojibake",
            "mixed entity/context marker stack",
            "ambiguous CK3 dynamic structure",
        ],
        "promotion_gate": "Read-only final micro-policy; do not create lifecycle/apply from this branch without a separate guarded prompt.",
        "observed_decision_counts": dict(decisions),
        "observed_secondary_marker_counts": dict(secondary_counts),
        "branch_action": branch_action,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only acclaimed knight entity requirement policy review.")
    parser.add_argument("--entity-jsonl", required=True, type=Path)
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    args = parser.parse_args()

    rows = source_rows(args.entity_jsonl)
    conn = connect_readonly()
    states = fetch_states(conn, args.segment_state_run_id, [int(row["segment_id"]) for row in rows])

    results: list[dict[str, Any]] = []
    decision_counts: Counter[str] = Counter()
    entity_counts: Counter[str] = Counter()
    unlock_counts: Counter[str] = Counter()
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
        entity_markers = markers(ENTITY_MARKERS, blob)
        unlock_markers = markers(UNLOCK_MARKERS, blob)
        requirement_markers = markers(REQUIREMENT_MARKERS, blob)
        secondary_markers = markers(SECONDARY_MARKERS, blob)
        decision, component, lifecycle, rationale = classify(state, entity_markers, unlock_markers, requirement_markers, secondary_markers)
        if decision not in ALLOWED_DECISIONS:
            raise SystemExit(f"unknown decision {decision} for segment_id {segment_id}")
        requires_apply_later = False
        lifecycle_later += int(lifecycle)
        apply_later += int(requires_apply_later)
        decision_counts[decision] += 1
        entity_counts.update(entity_markers or ["NoEntityMarker"])
        unlock_counts.update(unlock_markers or ["NoUnlockMarker"])
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
                "entity_markers": entity_markers,
                "unlock_markers": unlock_markers,
                "requirement_markers": requirement_markers,
                "secondary_markers": secondary_markers,
                "entity_requirement_decision": decision,
                "next_component": component,
                "requires_lifecycle_later": lifecycle,
                "requires_apply_later": requires_apply_later,
                "corrected_text": "",
                "rationale": rationale,
            }
        )

    if apply_later != 0:
        raise SystemExit(f"requires_apply_later must be 0, got {apply_later}")

    dominant, dominant_count = decision_counts.most_common(1)[0] if decision_counts else ("none", 0)
    branch_action = "recommend_last_narrow_prompt" if dominant_count >= 8 else "close_branch_return_to_requirement_tooltip"
    next_prompt = (
        "chat_exec_acclaimed_knight_entity_unlock_final_policy_prompt.md"
        if branch_action == "recommend_last_narrow_prompt"
        else "chat_exec_requirement_effect_list_multiline_subpolicy_review_prompt.md"
    )

    txt_path, jsonl_path, spec_path = output_paths()
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps({
            "record_type": "summary",
            "source_decision": SOURCE_DECISION,
            "total_reviewed": len(results),
            "pending_count": pending_count,
            "decision_counts": dict(decision_counts),
            "entity_marker_counts": dict(entity_counts),
            "unlock_marker_counts": dict(unlock_counts),
            "requirement_marker_counts": dict(req_counts),
            "secondary_marker_counts": dict(secondary_counts),
            "ready_lifecycle_future": lifecycle_later,
            "apply_candidates_future": apply_later,
            "dominant_subtype": dominant,
            "dominant_count": dominant_count,
            "branch_action": branch_action,
            "next_prompt": next_prompt,
        }, ensure_ascii=False, sort_keys=True) + "\n")
        for decision, count in decision_counts.most_common():
            handle.write(json.dumps({"record_type": "decision_count", "entity_requirement_decision": decision, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for marker, count in entity_counts.most_common():
            handle.write(json.dumps({"record_type": "entity_marker_count", "marker": marker, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for marker, count in unlock_counts.most_common():
            handle.write(json.dumps({"record_type": "unlock_marker_count", "marker": marker, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for marker, count in req_counts.most_common():
            handle.write(json.dumps({"record_type": "requirement_marker_count", "marker": marker, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for marker, count in secondary_counts.most_common():
            handle.write(json.dumps({"record_type": "secondary_marker_count", "marker": marker, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for priority, (prompt, rationale) in enumerate([
            (next_prompt, "dominant decision meets stop-rule threshold; optional final narrow policy"),
            ("chat_exec_requirement_effect_list_multiline_subpolicy_review_prompt.md", "recommended larger block after closing this micro-branch"),
            ("chat_exec_knight_attribute_maa_culture_policy_review_prompt.md", "sibling branch if returning to accolade only"),
        ], 1):
            handle.write(json.dumps({"record_type": "strategy", "priority": priority, "next_prompt": prompt, "rationale": rationale}, ensure_ascii=False, sort_keys=True) + "\n")
        for result in results:
            handle.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")

    with spec_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(build_spec(args.segment_state_run_id, decision_counts, entity_counts, secondary_counts, branch_action), handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("Acclaimed knight entity requirement policy review\n\n")
        handle.write(f"total_revisado: {len(results)}\n")
        handle.write(f"pending_count: {pending_count}\n")
        handle.write(f"ready_lifecycle_future: {lifecycle_later}\n")
        handle.write(f"apply_candidates_future: {apply_later}\n")
        handle.write(f"subtipo_dominante: {dominant}\n")
        handle.write(f"dominant_count: {dominant_count}\n")
        handle.write(f"branch_action: {branch_action}\n\n")
        handle.write("entity_requirement_decision_counts:\n")
        for decision, count in decision_counts.most_common():
            handle.write(f"- {decision}: {count}\n")
        handle.write("\nTop entity markers:\n")
        for marker, count in entity_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nTop unlock markers:\n")
        for marker, count in unlock_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nTop requirement markers:\n")
        for marker, count in req_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nTop secondary markers:\n")
        for marker, count in secondary_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nAnalise\n")
        handle.write("- acclaimed_knight_entity_requirement_policy deve virar conhecimento/spec read-only final deste micro-ramo.\n")
        handle.write("- Lifecycle/apply curto: nao; zero candidatos nesta revisao.\n")
        handle.write("- A cadeia requirement_tooltip -> scope_getter -> GetTrait -> accolade -> knight attribute -> unlock -> entity requirement deve ficar antes do parser generico.\n")
        if branch_action == "recommend_last_narrow_prompt":
            handle.write("- Regra de parada: ha decisao >= 8; um ultimo prompt estreito e permitido, mas nao necessario para apply.\n")
        else:
            handle.write("- Regra de parada: nenhuma decisao >= 8; encerrar ramo e voltar a bloco maior.\n")
        handle.write(f"- Proximo prompt recomendado: {next_prompt}.\n")

    print(f"txt: {txt_path}")
    print(f"jsonl: {jsonl_path}")
    print(f"spec: {spec_path}")
    print(f"total_reviewed: {len(results)}")
    print(f"pending_count: {pending_count}")
    print(f"ready_lifecycle_future: {lifecycle_later}")
    print(f"apply_candidates_future: {apply_later}")
    print(f"branch_action: {branch_action}")
    print("decision_counts:")
    for decision, count in decision_counts.most_common():
        print(f"  {decision}: {count}")


if __name__ == "__main__":
    main()
