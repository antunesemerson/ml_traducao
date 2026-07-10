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


TARGET_GENDER_DECISION = "gender_policy_needs_local_player_possessive_resolution"
TARGET_SELECT_DECISION = "select_cstring_needs_possessive_resolution"
MAX_TOTAL = 80

ALLOWED_DECISIONS = {
    "possessive_ready_false_reopen",
    "possessive_ready_lifecycle",
    "needs_local_player_possessive_policy",
    "needs_player_vs_target_possessive_policy",
    "needs_actor_target_possessive_policy",
    "needs_recipient_possessive_policy",
    "needs_dele_dela_policy",
    "needs_seu_sua_policy",
    "needs_teu_tua_policy",
    "needs_vosso_vossa_policy",
    "needs_name_nickname_guard_after_possessive",
    "needs_custom_loc_scope_parser_after_possessive",
    "needs_event_context_after_possessive",
    "needs_domain_context_after_possessive",
    "needs_residual_repair_after_possessive",
    "possessive_blocked_uncertain",
}

DELE_DELA_RE = re.compile(r"\bdele\b|\bdela\b", re.I)
SEU_SUA_RE = re.compile(r"\bseu\b|\bsua\b|\bseus\b|\bsuas\b|\bseu personagem\b", re.I)
TEU_TUA_RE = re.compile(r"\bteu\b|\btua\b|\bteus\b|\btuas\b", re.I)
VOSSO_RE = re.compile(r"\bvosso\b|\bvossa\b|\bvossos\b|\bvossas\b", re.I)
LOCAL_PLAYER_RE = re.compile(r"IsLocalPlayer|GetPlayer|LocalPlayer|GetLocalPlayer|\bvocê\b|\bvocês\b|\bmeu\b|\bminha\b", re.I)
PLAYER_TARGET_RE = re.compile(r"CHARACTER\.IsLocalPlayer|TARGET_CHARACTER\.IsLocalPlayer|Or\([^)]*IsLocalPlayer|And\([^)]*IsLocalPlayer", re.I)
ACTOR_TARGET_RE = re.compile(r"\bCHARACTER\.|\bTARGET_CHARACTER\.|\bROOT\.|\bFROM\.|\bTARGET\.|\bSCOPE\.|\bactor\b|\btarget\b", re.I)
RECIPIENT_RE = re.compile(r"recipient|addressee|destinat[aá]rio|para você|para ti|lhe\b", re.I)
NAME_RE = re.compile(r"GetShortUIName|GetName|GetDynasty|nickname|nick_|dynasty|house|nome|casa", re.I)
CUSTOM_SCOPE_RE = re.compile(r"Custom\(|Get[A-Za-z0-9_]+\(|PROVINCE\.|SCOPE\.|ROOT\.|TARGET\.|CHARACTER\.", re.I)
EVENT_RE = re.compile(r"event|\.desc|desc\.|option|coronation|lover|friend|gift|journey|activity|interaction|story|memory", re.I)
DOMAIN_RE = re.compile(r"religion|faith|culture|tradition|doctrine|artifact|title|law|government|contract|war|coroa[cç][aã]o", re.I)
RESIDUAL_RE = re.compile(r"\b(sentisteis|sintieron|buena mujer|buen hombre|se agradecem|the|your|you|their|has|will|cannot)\b|NÃ|Ãƒ|Â", re.I)
TOKEN_RE = re.compile(r"\[[^\]]+\]|\$[^$]+\$|Custom\(|Select_CString|SelectLocalization|Concept\(|ScriptValue|Get[A-Za-z0-9_]+\(", re.I)
POSSESSIVE_ANY_RE = re.compile(
    r"\bseu\b|\bsua\b|\bseus\b|\bsuas\b|\bdele\b|\bdela\b|\bteu\b|\btua\b|\bvosso\b|\bvossa\b|\bvocê\b|\bvocês\b",
    re.I,
)


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


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


def gather_sources(gender_jsonl: Path, select_cstring_jsonl: Path) -> tuple[list[dict[str, Any]], int, int, int]:
    raw: list[dict[str, Any]] = []
    consolidated = [row for row in read_jsonl(gender_jsonl) if row.get("record_type") == "sample_review"]
    for row in consolidated:
        if row.get("gender_policy_decision") == TARGET_GENDER_DECISION:
            item = dict(row)
            item["source_decision"] = row["gender_policy_decision"]
            item["source_review"] = "gender_local_player_policy_consolidated_review"
            item["sample_kind"] = "target"
            raw.append(item)
    for row in read_jsonl(select_cstring_jsonl):
        if row.get("record_type") == "sample_review" and row.get("select_cstring_decision") == TARGET_SELECT_DECISION:
            item = dict(row)
            item["source_decision"] = row["select_cstring_decision"]
            item["source_review"] = "gender_local_player_select_cstring_resolution_review"
            item["sample_kind"] = "target"
            raw.append(item)

    target_ids = {int(row["segment_id"]) for row in raw}
    extras: list[dict[str, Any]] = []
    for row in consolidated:
        if len(raw) + len(extras) >= MAX_TOTAL:
            break
        segment_id = int(row["segment_id"])
        if segment_id in target_ids:
            continue
        blob = " ".join(str(row.get(key) or "") for key in ("old_text", "confirmed_text", "output_text", "relative_path", "source_key"))
        if POSSESSIVE_ANY_RE.search(blob) or "pronoun/possessive surface" in row.get("gender_markers", []):
            item = dict(row)
            item["source_decision"] = row.get("gender_policy_decision", "")
            item["source_review"] = "diagnostic_extra_from_consolidated_possessive_marker"
            item["sample_kind"] = "diagnostic_extra"
            extras.append(item)

    all_rows = raw + extras
    dedup: dict[int, dict[str, Any]] = {}
    for row in all_rows:
        dedup.setdefault(int(row["segment_id"]), row)
    return list(dedup.values()), len(raw), len(all_rows) - len(dedup), len(extras)


def fetch_guards(conn: sqlite3.Connection, run_id: int, ledger_run_id: int, ids: list[int]) -> tuple[dict[int, dict[str, Any]], dict[int, tuple[str, ...]]]:
    if not ids:
        return {}, {}
    ph = ",".join("?" for _ in ids)
    states = {
        int(row["segment_id"]): dict(row)
        for row in conn.execute(
            f"""
            SELECT segment_id, state_group, is_closed, needs_output_apply, confirmed_matches_output, needs_reopen, final_state
            FROM segment_state_items
            WHERE run_id = ? AND segment_id IN ({ph})
            """,
            (run_id, *ids),
        )
    }
    fams: dict[int, set[str]] = {}
    for row in conn.execute(
        f"""
        SELECT segment_id, issue_family
        FROM ml_issue_ledger_items
        WHERE run_id = ? AND status = 'open' AND segment_id IN ({ph})
        """,
        (ledger_run_id, *ids),
    ):
        fams.setdefault(int(row["segment_id"]), set()).add(str(row["issue_family"]))
    return states, {sid: tuple(sorted(values)) for sid, values in fams.items()}


def state_ready(state: dict[str, Any] | None) -> bool:
    return bool(
        state
        and state.get("state_group") == "pending"
        and int(state.get("is_closed") or 0) == 0
        and int(state.get("needs_output_apply") or 0) == 0
        and int(state.get("confirmed_matches_output") or 0) == 1
    )


def marker_names(blob: str) -> tuple[list[str], list[str], list[str]]:
    possessive = []
    for name, pattern in {
        "dele/dela": DELE_DELA_RE,
        "seu/sua": SEU_SUA_RE,
        "teu/tua": TEU_TUA_RE,
        "vosso/vossa": VOSSO_RE,
    }.items():
        if pattern.search(blob):
            possessive.append(name)
    local = []
    if LOCAL_PLAYER_RE.search(blob):
        local.append("local_player")
    if PLAYER_TARGET_RE.search(blob):
        local.append("player_vs_target")
    scope = []
    for name, pattern in {
        "actor_target": ACTOR_TARGET_RE,
        "recipient": RECIPIENT_RE,
        "custom_scope": CUSTOM_SCOPE_RE,
    }.items():
        if pattern.search(blob):
            scope.append(name)
    return possessive, local, scope


def classify(row: dict[str, Any], state: dict[str, Any] | None) -> tuple[str, str, str]:
    blob = " ".join(str(row.get(key) or "") for key in ("relative_path", "source_key", "old_text", "confirmed_text", "output_text"))
    confirmed = str(row.get("confirmed_text") or "")
    output = str(row.get("output_text") or "")
    if not state_ready(state):
        return "possessive_blocked_uncertain", "blocked_uncertain", "state guard failed in selected segment-state run"
    if RESIDUAL_RE.search(blob):
        return "needs_residual_repair_after_possessive", "residual_after_possessive", "visible residual must wait for possessive perspective"
    if DELE_DELA_RE.search(blob):
        return "needs_dele_dela_policy", "dele_dela_policy", "dele/dela possessive form needs explicit policy"
    if SEU_SUA_RE.search(blob):
        if PLAYER_TARGET_RE.search(blob):
            return "needs_player_vs_target_possessive_policy", "player_vs_target_possessive", "seu/sua depends on player-vs-target perspective"
        return "needs_seu_sua_policy", "seu_sua_policy", "seu/sua possessive form needs explicit policy"
    if TEU_TUA_RE.search(blob):
        return "needs_teu_tua_policy", "teu_tua_policy", "teu/tua possessive form needs explicit policy"
    if VOSSO_RE.search(blob):
        return "needs_vosso_vossa_policy", "vosso_vossa_policy", "vosso/vossa possessive form needs explicit policy"
    if LOCAL_PLAYER_RE.search(blob):
        return "needs_local_player_possessive_policy", "local_player_possessive", "local-player perspective controls possessive form"
    if RECIPIENT_RE.search(blob):
        return "needs_recipient_possessive_policy", "recipient_possessive", "recipient/addressee controls possessive form"
    if ACTOR_TARGET_RE.search(blob):
        return "needs_actor_target_possessive_policy", "actor_target_possessive", "actor/target/root scope controls possessive form"
    if NAME_RE.search(blob):
        return "needs_name_nickname_guard_after_possessive", "name_guard_after_possessive", "name/nickname guard remains after possessive"
    if CUSTOM_SCOPE_RE.search(blob):
        return "needs_custom_loc_scope_parser_after_possessive", "custom_scope_after_possessive", "CustomLoc/scope parser remains after possessive"
    if EVENT_RE.search(blob):
        return "needs_event_context_after_possessive", "event_context_after_possessive", "event context remains after possessive"
    if DOMAIN_RE.search(blob):
        return "needs_domain_context_after_possessive", "domain_context_after_possessive", "domain context remains after possessive"
    if confirmed and output and confirmed == output and not TOKEN_RE.search(blob):
        return "possessive_ready_lifecycle", "ready_lifecycle", "confirmed/output aligned and no unresolved token remains"
    return "possessive_blocked_uncertain", "blocked_uncertain", "no safe possessive substage matched"


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_gender_local_player_possessive_resolution_review"
    spec = reports_dir / f"{stamp}_gender_local_player_possessive_resolution_spec.json"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), spec


def build_spec(run_id: int, ledger_run_id: int, decision_counts: Counter[str], possessive_counts: Counter[str]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "created_for": "read_only_policy_stage_design",
        "parent_policy": "gender_local_player_policy",
        "stage_id": "resolve_local_player_possessive",
        "segment_state_run_id": run_id,
        "ledger_run_id": ledger_run_id,
        "entry_conditions": ["possessive/pronoun marker", "local-player possessive marker", "Select_CString routed to possessive"],
        "possessive_forms": list(possessive_counts.keys()),
        "resolution_order": [
            "block residual until perspective is resolved",
            "detect explicit dele/dela and seu/sua forms",
            "resolve player-vs-target/local-player perspective",
            "route actor/target/recipient ambiguity",
            "route name/custom-scope/event/domain guards",
        ],
        "next_components": [
            "resolve_select_cstring_player",
            "actor_target_recipient_policy",
            "custom_loc_scope_parser",
            "event_context_composer",
            "domain_context_composer",
            "residual_dependency_filtered_repair",
        ],
        "blocked_conditions": ["visible residual", "actor/target ambiguity", "recipient ambiguity", "unparsed custom scope"],
        "promotion_gate": "read-only validation, zero false-safe sample, no apply until protected prompt",
        "observed_decision_counts": dict(decision_counts),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only local-player possessive resolution review.")
    parser.add_argument("--gender-jsonl", required=True, type=Path)
    parser.add_argument("--select-cstring-jsonl", required=True, type=Path)
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    parser.add_argument("--ledger-run-id", type=int, default=76)
    args = parser.parse_args()

    rows, raw_target_total, duplicates_removed, diagnostic_extras = gather_sources(args.gender_jsonl, args.select_cstring_jsonl)
    ids = [int(row["segment_id"]) for row in rows]
    conn = connect_readonly()
    states, families = fetch_guards(conn, args.segment_state_run_id, args.ledger_run_id, ids)
    results: list[dict[str, Any]] = []
    for row in rows:
        segment_id = int(row["segment_id"])
        decision, next_component, rationale = classify(row, states.get(segment_id))
        if decision not in ALLOWED_DECISIONS:
            raise SystemExit(f"invalid decision: {decision}")
        blob = " ".join(str(row.get(key) or "") for key in ("relative_path", "source_key", "old_text", "confirmed_text", "output_text"))
        possessive_markers, local_player_markers, scope_markers = marker_names(blob)
        results.append(
            {
                "record_type": "sample_review",
                "segment_id": segment_id,
                "relative_path": row.get("relative_path") or "",
                "source_key": row.get("source_key") or "",
                "families_open": list(families.get(segment_id, ())),
                "source_decision": row.get("source_decision") or "",
                "source_review": row.get("source_review") or "",
                "sample_kind": row.get("sample_kind") or "target",
                "old_text": row.get("old_text") or "",
                "confirmed_text": row.get("confirmed_text") or "",
                "output_text": row.get("output_text") or "",
                "possessive_markers": possessive_markers,
                "local_player_markers": local_player_markers,
                "scope_markers": scope_markers,
                "possessive_decision": decision,
                "next_component": next_component,
                "requires_lifecycle_later": decision in {"possessive_ready_false_reopen", "possessive_ready_lifecycle"},
                "requires_apply_later": False,
                "corrected_text": "",
                "rationale": rationale,
            }
        )
    if len(ids) != len(set(ids)):
        raise SystemExit("deduplication failed")
    if any(row["requires_apply_later"] for row in results):
        raise SystemExit("unexpected apply candidate")

    decision_counts = Counter(row["possessive_decision"] for row in results)
    possessive_counts = Counter(marker for row in results for marker in row["possessive_markers"])
    txt_path, jsonl_path, spec_path = output_paths()
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(
            json.dumps(
                {
                    "record_type": "summary",
                    "raw_target_total": raw_target_total,
                    "deduplicated_total": len(results),
                    "duplicates_removed": duplicates_removed,
                    "diagnostic_extras": diagnostic_extras,
                    "decision_counts": dict(decision_counts),
                    "possessive_counts": dict(possessive_counts),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )
        for row in results:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("Gender/local-player possessive resolution review\n\n")
        handle.write(f"raw_target_total: {raw_target_total}\n")
        handle.write(f"deduplicated_total: {len(results)}\n")
        handle.write(f"duplicates_removed: {duplicates_removed}\n")
        handle.write(f"diagnostic_extras: {diagnostic_extras}\n")
        handle.write(f"ready_lifecycle_future: {sum(1 for row in results if row['requires_lifecycle_later'])}\n")
        handle.write(f"future_apply_candidates: {sum(1 for row in results if row['requires_apply_later'])}\n")
        handle.write("possessive_decision_counts:\n")
        for decision, count in decision_counts.most_common():
            handle.write(f"- {decision}: {count}\n")
        handle.write("\npossessive_form_counts:\n")
        for marker, count in possessive_counts.most_common():
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nAnalise\n")
        handle.write("- resolve_local_player_possessive nao deve virar substage separado agora; deve ser incorporado ao Select_CString/local-player como microfase.\n")
        handle.write("- Nao gera lifecycle/apply no curto prazo; e roteador de perspectiva e formas possessivas.\n")
        handle.write("- Deve rodar depois de Select_CString/local-player e em paralelo aos ES helpers, antes de residual repair.\n")
        handle.write("- Proximo passo recomendado: parser-backed dynamic, depois requirement/effect, ou implementar spec read-only do router/policy.\n")
        handle.write("\nProximos prompts\n")
        handle.write("1. chat_exec_parser_backed_dynamic_expression_design_prompt.md\n")
        handle.write("2. chat_exec_requirement_effect_router_validation_prompt.md\n")
        handle.write("3. chat_exec_macro_lane_router_readonly_component_spec_prompt.md\n")
    spec = build_spec(args.segment_state_run_id, args.ledger_run_id, decision_counts, possessive_counts)
    with spec_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(spec, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    print(f"txt: {txt_path}")
    print(f"jsonl: {jsonl_path}")
    print(f"spec: {spec_path}")
    print(f"raw_target_total: {raw_target_total}")
    print(f"deduplicated_total: {len(results)}")
    print(f"diagnostic_extras: {diagnostic_extras}")
    print("possessive_decision_counts:")
    for decision, count in decision_counts.most_common():
        print(f"  {decision}: {count}")


if __name__ == "__main__":
    main()
