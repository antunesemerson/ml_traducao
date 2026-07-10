from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import macro_lane_router_architecture_review as router


TARGET_COHORTS = [
    (
        "02_dynamic_parser::dynamic_ck3_expression_microagent+gender_token_microagent::03_custom_loc_scope_getter+04_gender_local_player+05_actor_target_recipient",
        80,
    ),
    (
        "02_dynamic_parser::dynamic_ck3_expression_microagent+gender_token_microagent+nickname_name_policy::03_custom_loc_scope_getter+04_gender_local_player+05_actor_target_recipient",
        60,
    ),
    (
        "02_dynamic_parser::dynamic_ck3_expression_microagent+gender_token_microagent+semantic_review_router::03_custom_loc_scope_getter+04_gender_local_player+05_actor_target_recipient",
        60,
    ),
    (
        "02_dynamic_parser::dynamic_ck3_expression_microagent+gender_token_microagent::03_custom_loc_scope_getter+04_gender_local_player+06_requirement_tooltip",
        40,
    ),
]

ALLOWED_DECISIONS = {
    "gender_policy_ready_false_reopen",
    "gender_policy_ready_lifecycle",
    "gender_policy_needs_select_cstring_player_resolution",
    "gender_policy_needs_es_oa_resolution",
    "gender_policy_needs_es_xa_ea_resolution",
    "gender_policy_needs_el_la_del_dela_resolution",
    "gender_policy_needs_local_player_possessive_resolution",
    "gender_policy_needs_actor_target_resolution",
    "gender_policy_needs_recipient_resolution",
    "gender_policy_needs_custom_loc_scope_parser_after_gender",
    "gender_policy_needs_requirement_effect_after_gender",
    "gender_policy_needs_domain_context_after_gender",
    "gender_policy_needs_event_context_after_gender",
    "gender_policy_needs_name_nickname_guard",
    "gender_policy_needs_residual_repair_after_gender",
    "gender_policy_blocked_uncertain",
}

MARKERS = {
    "Select_CString": re.compile(r"Select_CString|SelectLocalization", re.I),
    "CustomLoc/Custom": re.compile(r"Custom\(|CustomLoc|custom_loc", re.I),
    "ES_OA": re.compile(r"ES_OA|ES_AO|\bES_A\b|\bES_O\b", re.I),
    "ES_XA_EA": re.compile(r"ES_XA|ES_EA|ES_XA_EA", re.I),
    "ES_ElLa/DelDela": re.compile(r"ES_ElLa|ES_DelDela|ES_AlAla", re.I),
    "GetPlayer/LocalPlayer": re.compile(r"GetPlayer|LocalPlayer|GetLocalPlayer", re.I),
    "ROOT/FROM/scope": re.compile(r"\bROOT\.|\bFROM\.|\bSCOPE\.|\bTARGET\.|\bCHARACTER\.|\bTHIS\.|\bscope\b", re.I),
    "pronoun/possessive surface": re.compile(r"\bvocê\b|\bvocês\b|\bseu\b|\bsua\b|\bseus\b|\bsuas\b|\bdele\b|\bdela\b|\bteu\b|\btua\b", re.I),
    "recipient/target": re.compile(r"recipient|target|addressee|destinat[aá]rio|TARGET|ROOT|FROM", re.I),
    "name/nickname/dynasty": re.compile(r"name|nickname|dynasty|house|GetName|GetDynasty|epithet|nick_", re.I),
}

REQUIREMENT_EFFECT_RE = re.compile(r"tooltip|_tt\b|requirement|required|unlock|trigger|\\n|\n|\$EFFECT_LIST_BULLET\$|#indent|effect", re.I)
DOMAIN_RE = re.compile(r"religion|faith|culture|tradition|doctrine|artifact|activity|title|law|government|nickname|dynasty", re.I)
EVENT_RE = re.compile(r"event|\.desc|desc\.|option|toast|dialogue|story|interaction|journey|travel|memory|scheme", re.I)
RESIDUAL_RE = re.compile(r"\b(the|your|you|their|has|will|cannot|consiguio|consiguió|ganaste|tendras|será|mas|más|muy|facil|fácil)\b|NÃ|Ãƒ|Â", re.I)
TOKEN_RE = re.compile(r"\[[^\]]+\]|\$[^$]+\$|Custom\(|Select_CString|Concept\(|ScriptValue|Get[A-Za-z0-9_]+\(", re.I)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"Invalid JSONL at {path}:{line_number}: {exc}") from exc
    return records


def parse_cohort_key(cohort_key: str) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    primary, families, secondaries = cohort_key.split("::", 2)
    return (
        primary,
        tuple(families.split("+") if families else []),
        tuple([] if secondaries == "no_secondary" else secondaries.split("+")),
    )


def route_key(item: dict[str, Any]) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    return item["primary_lane"], tuple(item["families"]), tuple(item["secondary_lanes"][:3])


def latest_confirmations(conn: Any, ids: list[int]) -> dict[int, str]:
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT segment_id, confirmed_text
        FROM segment_confirmations
        WHERE id IN (
            SELECT MAX(id)
            FROM segment_confirmations
            WHERE segment_id IN ({placeholders})
            GROUP BY segment_id
        )
        """,
        ids,
    ).fetchall()
    return {int(row["segment_id"]): str(row["confirmed_text"] or "") for row in rows}


def marker_list(text: str) -> list[str]:
    return [name for name, pattern in MARKERS.items() if pattern.search(text)]


def dynamic_markers(text: str) -> list[str]:
    markers = []
    for name, pattern in {
        "tokens": TOKEN_RE,
        "Custom": MARKERS["CustomLoc/Custom"],
        "Select_CString": MARKERS["Select_CString"],
        "scope": MARKERS["ROOT/FROM/scope"],
        "GetPlayer": MARKERS["GetPlayer/LocalPlayer"],
    }.items():
        if pattern.search(text):
            markers.append(name)
    return markers


def classify(item: dict[str, Any], old_text: str, confirmed_text: str, output_text: str) -> tuple[str, str, str]:
    blob = " ".join([item["relative_path"], item["source_key"], old_text, confirmed_text, output_text])
    genders = marker_list(blob)

    if RESIDUAL_RE.search(blob):
        return "gender_policy_needs_residual_repair_after_gender", "residual_after_gender", "visible residual should only be repaired after gender/context routing"
    if MARKERS["Select_CString"].search(blob) or MARKERS["GetPlayer/LocalPlayer"].search(blob):
        return (
            "gender_policy_needs_select_cstring_player_resolution",
            "resolve_select_cstring_player",
            "Select_CString/GetPlayer/local-player marker controls person or gender choice",
        )
    if MARKERS["ES_XA_EA"].search(blob):
        return "gender_policy_needs_es_xa_ea_resolution", "resolve_es_xa_ea", "ES_XA/ES_EA helper requires gender helper policy"
    if MARKERS["ES_ElLa/DelDela"].search(blob):
        return (
            "gender_policy_needs_el_la_del_dela_resolution",
            "resolve_el_la_del_dela",
            "ES_ElLa/DelDela/AlAla article helper requires gender article policy",
        )
    if MARKERS["ES_OA"].search(blob):
        return "gender_policy_needs_es_oa_resolution", "resolve_es_oa", "ES_OA/o-a helper requires gender helper policy"
    if MARKERS["pronoun/possessive surface"].search(output_text):
        return (
            "gender_policy_needs_local_player_possessive_resolution",
            "resolve_local_player_possessive",
            "possessive/pronoun surface points to local-player perspective",
        )
    if MARKERS["recipient/target"].search(blob):
        return (
            "gender_policy_needs_actor_target_resolution",
            "resolve_actor_target",
            "gender depends on actor/target/root/from scope",
        )
    if MARKERS["name/nickname/dynasty"].search(blob):
        return (
            "gender_policy_needs_name_nickname_guard",
            "name_nickname_guard",
            "name/nickname/dynasty guard is needed after gender detection",
        )
    if REQUIREMENT_EFFECT_RE.search(blob):
        return (
            "gender_policy_needs_requirement_effect_after_gender",
            "route_requirement_effect_after_gender",
            "requirement/effect surface remains after gender routing",
        )
    if DOMAIN_RE.search(blob):
        return (
            "gender_policy_needs_domain_context_after_gender",
            "route_domain_after_gender",
            "domain context remains after gender routing",
        )
    if EVENT_RE.search(blob):
        return (
            "gender_policy_needs_event_context_after_gender",
            "route_event_after_gender",
            "event/narrative context remains after gender routing",
        )
    if MARKERS["CustomLoc/Custom"].search(blob) or MARKERS["ROOT/FROM/scope"].search(blob):
        return (
            "gender_policy_needs_custom_loc_scope_parser_after_gender",
            "route_custom_scope_after_gender",
            "custom loc/scope parser is needed after gender marker detection",
        )
    if confirmed_text and output_text and confirmed_text == output_text and not TOKEN_RE.search(blob):
        return "gender_policy_ready_lifecycle", "ready_lifecycle_after_gender", "confirmed/output is aligned and no further dynamic/context dependency is visible"
    if genders:
        return (
            "gender_policy_needs_custom_loc_scope_parser_after_gender",
            "route_after_gender_unspecified_dynamic",
            "gender marker exists but remaining dynamic dependency needs parser",
        )
    return "gender_policy_blocked_uncertain", "uncertain_gender_policy", "no allowed gender/local-player substage matched safely"


def sample_items(routed: dict[int, dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for cohort_key, cohort_limit in TARGET_COHORTS:
        wanted = parse_cohort_key(cohort_key)
        matches = [item for item in routed.values() if route_key(item) == wanted]
        matches.sort(key=lambda item: (item["relative_path"], item["source_key"], item["segment_id"]))
        for item in matches[:cohort_limit]:
            if len(samples) >= limit:
                return samples
            item = dict(item)
            item["cohort_key"] = cohort_key
            item["cohort_universe"] = len(matches)
            samples.append(item)
    return samples


def build_spec(segment_state_run_id: int, ledger_run_id: int, marker_counts: Counter[str]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "created_for": "read_only_policy_design",
        "segment_state_run_id": segment_state_run_id,
        "ledger_run_id": ledger_run_id,
        "policy_id": "gender_local_player_policy",
        "entry_conditions": [
            "gender_token_microagent open family",
            "Select_CString or SelectLocalization marker",
            "ES_OA/ES_XA_EA/ES_ElLa/ES_DelDela helper marker",
            "GetPlayer/LocalPlayer or local-player pronoun/possessive surface",
        ],
        "stages": [
            {
                "id": "detect_gender_markers",
                "purpose": "Identify explicit gender/local-player markers before generic dynamic parsing.",
                "markers": [name for name, count in marker_counts.most_common() if count > 0],
            },
            {
                "id": "resolve_local_player_perspective",
                "purpose": "Separate player-facing pronoun/possessive/Select_CString decisions from actor-target scope decisions.",
                "guards": [
                    "confirmed_text must equal output_text for ready decisions",
                    "do not apply when actor/target/recipient is unresolved",
                    "preserve all CK3 tokens exactly",
                ],
            },
            {
                "id": "route_after_gender",
                "purpose": "Route unresolved cases to parser, requirement/effect, domain/event, or name guard after gender stage.",
                "next_components": [
                    "custom_loc_scope_parser",
                    "requirement_effect_list_policy",
                    "domain_context_composer",
                    "event_context_composer",
                    "name_nickname_guard",
                    "residual_dependency_filtered_repair",
                ],
            },
        ],
        "blocked_conditions": [
            "confirmed/output mismatch",
            "visible residual before gender/context resolved",
            "recipient/actor/target ambiguity",
            "domain/event context required",
        ],
        "promotion_gate": "read-only cohort validation, zero false-safe sample, no apply until explicit protected lifecycle/apply prompt",
    }


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_gender_local_player_policy_consolidated_review"
    spec = reports_dir / f"{stamp}_gender_local_player_policy_spec.json"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), spec


def write_jsonl(path: Path, rows: list[dict[str, Any]], cohort_universe: Counter[str], marker_counts: Counter[str]) -> None:
    decision_counts = Counter(row["gender_policy_decision"] for row in rows)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(
            json.dumps(
                {
                    "record_type": "summary",
                    "total_reviewed": len(rows),
                    "decision_counts": dict(decision_counts),
                    "marker_counts": dict(marker_counts),
                    "cohort_universe": dict(cohort_universe),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )
        for cohort, count in cohort_universe.items():
            handle.write(json.dumps({"record_type": "cohort_summary", "cohort_key": cohort, "estimated_universe": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def write_txt(path: Path, rows: list[dict[str, Any]], cohort_universe: Counter[str], marker_counts: Counter[str]) -> None:
    decision_counts = Counter(row["gender_policy_decision"] for row in rows)
    parser_after = sum(1 for row in rows if row["gender_policy_decision"] == "gender_policy_needs_custom_loc_scope_parser_after_gender")
    requirement_after = sum(1 for row in rows if row["gender_policy_decision"] == "gender_policy_needs_requirement_effect_after_gender")
    context_after = sum(1 for row in rows if row["gender_policy_decision"] in {"gender_policy_needs_domain_context_after_gender", "gender_policy_needs_event_context_after_gender"})
    ready = sum(1 for row in rows if row["requires_lifecycle_later"])
    apply = sum(1 for row in rows if row["requires_apply_later"])
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("Gender/local-player policy consolidated review\n\n")
        handle.write("Distribuicao da policy\n")
        handle.write(f"total_reviewed: {len(rows)}\n")
        handle.write(f"ready_lifecycle_future: {ready}\n")
        handle.write(f"future_apply_candidates: {apply}\n")
        handle.write(f"needs_parser_after_gender: {parser_after}\n")
        handle.write(f"needs_requirement_effect_after_gender: {requirement_after}\n")
        handle.write(f"needs_event_domain_after_gender: {context_after}\n")
        handle.write("cohort_universe:\n")
        for cohort, count in cohort_universe.items():
            handle.write(f"- {cohort}: {count}\n")
        handle.write("decision_counts:\n")
        for decision, count in decision_counts.most_common():
            handle.write(f"- {decision}: {count}\n")
        handle.write("\nMarkers encontrados\n")
        for marker, count in marker_counts.most_common():
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nAvaliacao da prioridade\n")
        handle.write("- gender_local_player_policy deve ser o primeiro componente real para estes cohorts, antes do parser dinamico generico.\n")
        handle.write("- Primeiro substage: resolve_select_cstring_player, seguido por ES helpers e actor/target.\n")
        handle.write("- Parser dinamico ainda vem depois para casos Custom/scope que sobraram apos detectar genero.\n")
        handle.write("- Este componente e principalmente roteador/composer no curto prazo; nao deve gerar apply agora.\n")
        handle.write("\nProximos prompts\n")
        handle.write("1. chat_exec_gender_local_player_select_cstring_resolution_prompt.md\n")
        handle.write("2. chat_exec_gender_local_player_es_helper_resolution_prompt.md\n")
        handle.write("3. chat_exec_parser_backed_dynamic_expression_design_prompt.md\n")
        handle.write("\nValidacoes\n")
        handle.write("- Banco aberto em modo read-only.\n")
        handle.write("- Spec JSON gerado como artefato de design, nao integrado ao pipeline.\n")
        handle.write("- Sem lifecycle, apply, segment-state, issue-ledger, confirmations, production, reindex, treino ou source/output changes.\n")


def validate_pending(conn: Any, ids: list[int], run_id: int) -> None:
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    count = conn.execute(
        f"""
        SELECT COUNT(*)
        FROM segment_state_items
        WHERE run_id = ?
          AND segment_id IN ({placeholders})
          AND state_group = 'pending'
          AND COALESCE(is_closed, 0) = 0
        """,
        (run_id, *ids),
    ).fetchone()[0]
    if count != len(set(ids)):
        raise SystemExit(f"pending validation mismatch: expected {len(set(ids))}, got {count}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only consolidated gender/local-player policy review.")
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    parser.add_argument("--ledger-run-id", required=True, type=int)
    parser.add_argument("--validation-jsonl", required=True, type=Path)
    parser.add_argument("--limit", type=int, default=240)
    args = parser.parse_args()

    read_jsonl(args.validation_jsonl)
    conn = router.connect_readonly()
    router.fetch_run(conn, "segment_state_runs", args.segment_state_run_id)
    router.fetch_run(conn, "ml_issue_ledger_runs", args.ledger_run_id)
    pending_rows = router.fetch_pending_rows(conn, args.segment_state_run_id, args.ledger_run_id)
    grouped = router.group_by_segment(pending_rows)
    routed = router.route_segments(grouped)
    samples = sample_items(routed, args.limit)
    ids = [int(item["segment_id"]) for item in samples]
    validate_pending(conn, ids, args.segment_state_run_id)
    confirmations = latest_confirmations(conn, sorted(set(ids)))

    rows: list[dict[str, Any]] = []
    marker_counts: Counter[str] = Counter()
    cohort_universe: Counter[str] = Counter()
    for item in samples:
        segment_rows = grouped[int(item["segment_id"])]
        first = segment_rows[0]
        old_text = str(first.get("old_text") or "")
        output_text = str(first.get("output_text") or "")
        confirmed_text = confirmations.get(int(item["segment_id"]), output_text)
        blob = " ".join([old_text, confirmed_text, output_text, item["relative_path"], item["source_key"]])
        markers = marker_list(blob)
        for marker in markers:
            marker_counts[marker] += 1
        decision, stage, rationale = classify(item, old_text, confirmed_text, output_text)
        if decision not in ALLOWED_DECISIONS:
            raise SystemExit(f"invalid decision for segment_id={item['segment_id']}: {decision}")
        cohort_universe[item["cohort_key"]] = int(item["cohort_universe"])
        rows.append(
            {
                "record_type": "sample_review",
                "segment_id": int(item["segment_id"]),
                "relative_path": item["relative_path"],
                "source_key": item["source_key"],
                "families_open": item["families"],
                "cohort_key": item["cohort_key"],
                "old_text": old_text,
                "confirmed_text": confirmed_text,
                "output_text": output_text,
                "gender_markers": markers,
                "dynamic_markers": dynamic_markers(blob),
                "recommended_policy_stage": stage,
                "gender_policy_decision": decision,
                "requires_lifecycle_later": decision in {"gender_policy_ready_false_reopen", "gender_policy_ready_lifecycle"},
                "requires_apply_later": False,
                "corrected_text": "",
                "rationale": rationale,
            }
        )

    if len({row["segment_id"] for row in rows}) != len(rows):
        raise SystemExit("duplicate segment_id in review sample")

    txt_path, jsonl_path, spec_path = output_paths()
    write_txt(txt_path, rows, cohort_universe, marker_counts)
    write_jsonl(jsonl_path, rows, cohort_universe, marker_counts)
    spec = build_spec(args.segment_state_run_id, args.ledger_run_id, marker_counts)
    with spec_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(spec, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    print(f"txt: {txt_path}")
    print(f"jsonl: {jsonl_path}")
    print(f"spec: {spec_path}")
    print(f"total_reviewed: {len(rows)}")
    print("gender_policy_decision_counts:")
    for decision, count in Counter(row["gender_policy_decision"] for row in rows).most_common():
        print(f"  {decision}: {count}")
    print("marker_counts:")
    for marker, count in marker_counts.most_common():
        print(f"  {marker}: {count}")


if __name__ == "__main__":
    main()
