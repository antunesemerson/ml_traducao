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


GENDER_SOURCE_DECISIONS = {
    "gender_policy_needs_el_la_del_dela_resolution",
    "gender_policy_needs_es_oa_resolution",
    "gender_policy_needs_es_xa_ea_resolution",
}
SELECT_SOURCE_DECISION = "select_cstring_needs_es_helper_resolution"

ALLOWED_DECISIONS = {
    "es_helper_ready_false_reopen",
    "es_helper_ready_lifecycle",
    "es_helper_needs_oa_resolution",
    "es_helper_needs_xa_ea_resolution",
    "es_helper_needs_el_la_resolution",
    "es_helper_needs_del_dela_resolution",
    "es_helper_needs_article_preposition_policy",
    "es_helper_needs_local_player_perspective",
    "es_helper_needs_player_vs_target_perspective",
    "es_helper_needs_actor_target_resolution",
    "es_helper_needs_recipient_resolution",
    "es_helper_needs_select_cstring_resolution",
    "es_helper_needs_custom_loc_scope_parser",
    "es_helper_needs_name_nickname_guard",
    "es_helper_needs_title_law_domain_context",
    "es_helper_needs_event_context",
    "es_helper_needs_residual_repair_after_resolution",
    "es_helper_blocked_uncertain",
}

ES_OA_RE = re.compile(r"ES_OA|ES_AO|\bES_A\b|\bES_O\b", re.I)
ES_XA_EA_RE = re.compile(r"ES_XA|ES_EA|ES_XA_EA", re.I)
ES_EL_LA_RE = re.compile(r"ES_ElLa", re.I)
ES_DEL_DELA_RE = re.compile(r"ES_DelDela|ES_AlAla", re.I)
ARTICLE_RE = re.compile(r"ElLa|DelDela|AlAla|artigo|preposi", re.I)
LOCAL_PLAYER_RE = re.compile(r"IsLocalPlayer|GetPlayer|LocalPlayer|GetLocalPlayer|\bvocê\b|\bvocês\b", re.I)
PLAYER_TARGET_RE = re.compile(r"CHARACTER\.IsLocalPlayer|TARGET_CHARACTER\.IsLocalPlayer|Or\([^)]*IsLocalPlayer|And\([^)]*IsLocalPlayer", re.I)
ACTOR_TARGET_RE = re.compile(r"\bCHARACTER\.|\bTARGET_CHARACTER\.|\bROOT\.|\bFROM\.|\bTARGET\.|\bSCOPE\.|\bactor\b|\btarget\b", re.I)
RECIPIENT_RE = re.compile(r"recipient|addressee|destinat[aá]rio", re.I)
SELECT_RE = re.compile(r"Select_CString|SelectLocalization", re.I)
CUSTOM_SCOPE_RE = re.compile(r"Custom\(|Get[A-Za-z0-9_]+\(|PROVINCE\.|SCOPE\.|ROOT\.|TARGET\.|CHARACTER\.", re.I)
NAME_RE = re.compile(r"GetShortUIName|GetName|GetDynasty|nickname|nick_|dynasty|house|nome|casa", re.I)
TITLE_DOMAIN_RE = re.compile(r"title|law|government|realm|religion|faith|culture|tradition|doctrine|artifact|contract|war|coroa[cç][aã]o", re.I)
EVENT_RE = re.compile(r"event|\.desc|desc\.|option|coronation|lover|friend|gift|journey|activity|interaction|story|memory", re.I)
RESIDUAL_RE = re.compile(r"\b(sentisteis|sintieron|buena mujer|buen hombre|se agradecem|the|your|you|their|has|will|cannot)\b|NÃ|Ãƒ|Â", re.I)


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


def gather_sources(gender_jsonl: Path, select_cstring_jsonl: Path) -> tuple[list[dict[str, Any]], int, int]:
    raw: list[dict[str, Any]] = []
    for row in read_jsonl(gender_jsonl):
        if row.get("record_type") == "sample_review" and row.get("gender_policy_decision") in GENDER_SOURCE_DECISIONS:
            item = dict(row)
            item["source_decision"] = row["gender_policy_decision"]
            item["source_review"] = "gender_local_player_policy_consolidated_review"
            raw.append(item)
    for row in read_jsonl(select_cstring_jsonl):
        if row.get("record_type") == "sample_review" and row.get("select_cstring_decision") == SELECT_SOURCE_DECISION:
            item = dict(row)
            item["source_decision"] = row["select_cstring_decision"]
            item["source_review"] = "gender_local_player_select_cstring_resolution_review"
            raw.append(item)
    dedup: dict[int, dict[str, Any]] = {}
    for row in raw:
        segment_id = int(row["segment_id"])
        dedup.setdefault(segment_id, row)
    return list(dedup.values()), len(raw), len(raw) - len(dedup)


def fetch_guards(conn: sqlite3.Connection, run_id: int, ledger_run_id: int, ids: list[int]) -> tuple[dict[int, dict[str, Any]], dict[int, tuple[str, ...]]]:
    if not ids:
        return {}, {}
    ph = ",".join("?" for _ in ids)
    states = {
        int(row["segment_id"]): dict(row)
        for row in conn.execute(
            f"""
            SELECT segment_id, state_group, is_closed, needs_output_apply, confirmed_matches_output
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


def marker_names(blob: str) -> list[str]:
    checks = {
        "ES_OA": ES_OA_RE,
        "ES_XA_EA": ES_XA_EA_RE,
        "ES_ElLa": ES_EL_LA_RE,
        "ES_DelDela/AlAla": ES_DEL_DELA_RE,
        "Select_CString": SELECT_RE,
        "LocalPlayer": LOCAL_PLAYER_RE,
        "actor_target": ACTOR_TARGET_RE,
        "recipient": RECIPIENT_RE,
        "custom_scope": CUSTOM_SCOPE_RE,
        "name_nickname": NAME_RE,
        "title_domain": TITLE_DOMAIN_RE,
        "event": EVENT_RE,
    }
    return [name for name, pattern in checks.items() if pattern.search(blob)]


def classify(row: dict[str, Any], state: dict[str, Any] | None) -> tuple[str, str, str]:
    blob = " ".join(str(row.get(key) or "") for key in ("relative_path", "source_key", "old_text", "confirmed_text", "output_text"))
    if not state_ready(state):
        return "es_helper_blocked_uncertain", "blocked_uncertain", "state guard failed in selected segment-state run"
    if RESIDUAL_RE.search(blob):
        return "es_helper_needs_residual_repair_after_resolution", "residual_after_es", "visible residual must wait for ES helper resolution"
    if ES_EL_LA_RE.search(blob):
        return "es_helper_needs_el_la_resolution", "resolve_el_la", "ES_ElLa article helper is explicit"
    if ES_DEL_DELA_RE.search(blob):
        return "es_helper_needs_del_dela_resolution", "resolve_del_dela", "ES_DelDela/AlAla preposition article helper is explicit"
    if ES_OA_RE.search(blob):
        return "es_helper_needs_oa_resolution", "resolve_oa", "ES_OA/o-a helper is explicit"
    if ES_XA_EA_RE.search(blob):
        return "es_helper_needs_xa_ea_resolution", "resolve_xa_ea", "ES_XA/ES_EA helper is explicit"
    if PLAYER_TARGET_RE.search(blob):
        return "es_helper_needs_player_vs_target_perspective", "player_vs_target_after_es", "player-vs-target branch affects helper resolution"
    if LOCAL_PLAYER_RE.search(blob):
        return "es_helper_needs_local_player_perspective", "local_player_after_es", "local-player perspective affects helper resolution"
    if RECIPIENT_RE.search(blob):
        return "es_helper_needs_recipient_resolution", "recipient_after_es", "recipient/addressee affects helper resolution"
    if ACTOR_TARGET_RE.search(blob):
        return "es_helper_needs_actor_target_resolution", "actor_target_after_es", "actor/target/root scope affects helper resolution"
    if SELECT_RE.search(blob):
        return "es_helper_needs_select_cstring_resolution", "select_cstring_after_es", "Select_CString must be resolved with helper"
    if CUSTOM_SCOPE_RE.search(blob):
        return "es_helper_needs_custom_loc_scope_parser", "custom_scope_after_es", "CustomLoc/scope parser needed after helper"
    if NAME_RE.search(blob):
        return "es_helper_needs_name_nickname_guard", "name_guard_after_es", "name/nickname/dynasty guard needed"
    if TITLE_DOMAIN_RE.search(blob):
        return "es_helper_needs_title_law_domain_context", "domain_after_es", "title/law/domain context needed"
    if EVENT_RE.search(blob):
        return "es_helper_needs_event_context", "event_after_es", "event context needed"
    if ARTICLE_RE.search(blob):
        return "es_helper_needs_article_preposition_policy", "article_preposition_policy", "generic gendered article/preposition policy needed"
    return "es_helper_blocked_uncertain", "blocked_uncertain", "no safe ES helper substage matched"


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_gender_local_player_es_helper_resolution_review"
    spec = reports_dir / f"{stamp}_gender_local_player_es_helper_resolution_spec.json"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), spec


def build_spec(run_id: int, ledger_run_id: int, decision_counts: Counter[str], helper_counts: Counter[str]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "created_for": "read_only_policy_stage_design",
        "parent_policy": "gender_local_player_policy",
        "stage_id": "resolve_es_helpers",
        "segment_state_run_id": run_id,
        "ledger_run_id": ledger_run_id,
        "entry_conditions": ["ES_OA/ES_AO", "ES_XA/ES_EA", "ES_ElLa", "ES_DelDela/ES_AlAla", "IsFemale/IsMale branch routed from Select_CString"],
        "helper_types": list(helper_counts.keys()),
        "resolution_order": [
            "resolve explicit ElLa/DelDela article helpers",
            "resolve explicit OA helpers",
            "resolve XA/EA helpers",
            "then route local-player, actor-target, custom-scope, name/domain/event guards",
        ],
        "next_components": [
            "local_player_perspective_policy",
            "actor_target_recipient_policy",
            "custom_loc_scope_parser",
            "name_nickname_guard",
            "title_law_domain_context",
            "event_context_composer",
            "residual_dependency_filtered_repair",
        ],
        "blocked_conditions": ["visible residual", "unresolved actor/target", "unresolved local-player perspective", "custom scope stack without parser"],
        "promotion_gate": "read-only validation, zero false-safe sample, no apply until protected prompt",
        "observed_decision_counts": dict(decision_counts),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only ES helper resolution review.")
    parser.add_argument("--gender-jsonl", required=True, type=Path)
    parser.add_argument("--select-cstring-jsonl", required=True, type=Path)
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    parser.add_argument("--ledger-run-id", type=int, default=76)
    args = parser.parse_args()

    rows, raw_total, duplicates_removed = gather_sources(args.gender_jsonl, args.select_cstring_jsonl)
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
        marks = marker_names(blob)
        results.append(
            {
                "record_type": "sample_review",
                "segment_id": segment_id,
                "relative_path": row.get("relative_path") or "",
                "source_key": row.get("source_key") or "",
                "families_open": list(families.get(segment_id, ())),
                "source_decision": row.get("source_decision") or "",
                "source_review": row.get("source_review") or "",
                "old_text": row.get("old_text") or "",
                "confirmed_text": row.get("confirmed_text") or "",
                "output_text": row.get("output_text") or "",
                "es_helper_markers": [m for m in marks if m.startswith("ES_")],
                "local_player_markers": [m for m in marks if m in {"LocalPlayer"}],
                "scope_markers": [m for m in marks if m in {"actor_target", "recipient", "custom_scope"}],
                "es_helper_decision": decision,
                "next_component": next_component,
                "requires_lifecycle_later": decision in {"es_helper_ready_false_reopen", "es_helper_ready_lifecycle"},
                "requires_apply_later": False,
                "corrected_text": "",
                "rationale": rationale,
            }
        )
    if len(ids) != len(set(ids)):
        raise SystemExit("deduplication failed")
    if any(row["requires_apply_later"] for row in results):
        raise SystemExit("unexpected apply candidate")

    decision_counts = Counter(row["es_helper_decision"] for row in results)
    helper_counts = Counter(marker for row in results for marker in row["es_helper_markers"])
    txt_path, jsonl_path, spec_path = output_paths()
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(
            json.dumps(
                {
                    "record_type": "summary",
                    "raw_total": raw_total,
                    "deduplicated_total": len(results),
                    "duplicates_removed": duplicates_removed,
                    "decision_counts": dict(decision_counts),
                    "helper_counts": dict(helper_counts),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )
        for row in results:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("Gender/local-player ES helper resolution review\n\n")
        handle.write(f"raw_total: {raw_total}\n")
        handle.write(f"deduplicated_total: {len(results)}\n")
        handle.write(f"duplicates_removed: {duplicates_removed}\n")
        handle.write(f"ready_lifecycle_future: {sum(1 for row in results if row['requires_lifecycle_later'])}\n")
        handle.write(f"future_apply_candidates: {sum(1 for row in results if row['requires_apply_later'])}\n")
        handle.write("es_helper_decision_counts:\n")
        for decision, count in decision_counts.most_common():
            handle.write(f"- {decision}: {count}\n")
        handle.write("\nhelper_counts:\n")
        for helper, count in helper_counts.most_common():
            handle.write(f"- {helper}: {count}\n")
        handle.write("\nAnalise\n")
        handle.write("- resolve_es_helpers deve virar substage read-only real.\n")
        handle.write("- Nao gera lifecycle/apply no curto prazo; e roteador de helpers e guards.\n")
        handle.write("- Helper dominante: ElLa/DelDela, seguido por ES_OA.\n")
        handle.write("- Confirma gender/local-player antes do parser generico para helpers explicitos.\n")
        handle.write("- Proximo prompt recomendado: possessive/local-player, depois parser-backed dynamic e requirement/effect.\n")
        handle.write("\nProximos prompts\n")
        handle.write("1. chat_exec_gender_local_player_possessive_resolution_prompt.md\n")
        handle.write("2. chat_exec_parser_backed_dynamic_expression_design_prompt.md\n")
        handle.write("3. chat_exec_requirement_effect_router_validation_prompt.md\n")
    spec = build_spec(args.segment_state_run_id, args.ledger_run_id, decision_counts, helper_counts)
    with spec_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(spec, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    print(f"txt: {txt_path}")
    print(f"jsonl: {jsonl_path}")
    print(f"spec: {spec_path}")
    print(f"raw_total: {raw_total}")
    print(f"deduplicated_total: {len(results)}")
    print(f"duplicates_removed: {duplicates_removed}")
    print("es_helper_decision_counts:")
    for decision, count in decision_counts.most_common():
        print(f"  {decision}: {count}")


if __name__ == "__main__":
    main()
