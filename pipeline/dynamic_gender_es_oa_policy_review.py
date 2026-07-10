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


ALLOWED_DECISIONS = {
    "es_oa_ready_false_reopen",
    "es_oa_ready_lifecycle",
    "es_oa_final_vowel_trim_repair_candidate",
    "es_oa_adjective_agreement_repair_candidate",
    "needs_es_oa_target_gender_policy",
    "needs_es_oa_title_trait_policy",
    "needs_es_oa_select_cstring_policy",
    "needs_es_oa_custom_loc_policy",
    "needs_es_oa_xa_ea_or_ella_deldela_policy",
    "needs_es_oa_event_context_composer",
    "needs_es_oa_domain_context",
    "needs_es_oa_residual_repair",
    "needs_new_microagent",
    "blocked_uncertain",
}

TOKEN_RE = re.compile(
    r"Select_CString|Custom\(|ES_(?:OA|XA|EA|ElLa|DelDela|AlAla)|\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_:.{};,|]+|#!",
    re.IGNORECASE,
)
ES_OA_RE = re.compile(r"Custom\(\s*['\"]ES_OA['\"]\s*\)|ES_OA", re.IGNORECASE)
MIXED_ES_RE = re.compile(r"ES_(?:XA|EA|ElLa|DelDela|AlAla)", re.IGNORECASE)
SELECT_RE = re.compile(r"Select_CString\s*\(", re.IGNORECASE)
TITLE_TRAIT_RE = re.compile(
    r"trait|accolade|knight|title|court_position|council|diarch|spouse|guardian|ward|hostage|GirlBoy|WomanMan|LadyLord|QueenKing",
    re.IGNORECASE,
)
EVENT_RE = re.compile(
    r"event|\.desc|desc\.|_key$|_log$|activity|activities|yearly|interaction|festival|feast|hunt|pilgrimage|coronation|wedding|tour|journey|roaming|contract|board_game|bp\d|ep\d|fp\d|ce\d|travel|diary|story",
    re.IGNORECASE,
)
DOMAIN_RE = re.compile(
    r"culture|cultural|faith|religion|doctrine|dynasty|house|realm|county|duchy|kingdom|empire|vassal|liege|scheme|secret|artifact|court|councillor|lifestyle|perk|struggle|war|battle|army|province|holding|law|government",
    re.IGNORECASE,
)
RESIDUAL_RE = re.compile(
    r"\b(?:vocę|nă|năo|săo|estăo|entăo|serăo|relaçăo|opçăo|crian[çc]a|menina|menino|niñ[ao]|reyes|reinas|ella|della|mism[oa]|adult[ao]|amig[ao]|querid[ao]|aterrorizad[ao]|convencid[ao]|acordad[ao])\b",
    re.IGNORECASE,
)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
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


def collect_source_rows(path_value: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for row in read_jsonl(db.project_path(path_value)):
        decision = row.get("dynamic_gender_decision") or row.get("decision")
        if decision != "needs_es_oa_policy":
            continue
        segment_id = int(row["segment_id"])
        if segment_id in seen:
            continue
        seen.add(segment_id)
        rows.append(row)
    return rows


def fetch_states(conn: sqlite3.Connection, run_id: int, segment_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not segment_ids:
        return {}
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT segment_id, final_state, state_group, needs_output_apply,
               confirmed_matches_output, needs_reopen, is_closed
        FROM segment_state_items
        WHERE run_id = ?
          AND segment_id IN ({placeholders})
        """,
        (run_id, *segment_ids),
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def tokens_seen(text: str) -> list[str]:
    labels: list[str] = []
    for label, pattern in [
        ("ES_OA", ES_OA_RE),
        ("MixedES", MIXED_ES_RE),
        ("Select_CString", SELECT_RE),
        ("CK3DynamicToken", re.compile(r"\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_:.{};,|]+|#!")),
        ("CustomLoc", re.compile(r"Custom\(", re.IGNORECASE)),
    ]:
        if pattern.search(text):
            labels.append(label)
    return labels


def state_is_ready_surface(state: dict[str, Any] | None) -> bool:
    if not state:
        return False
    return (
        state.get("state_group") == "pending"
        and int(state.get("needs_output_apply") or 0) == 0
        and int(state.get("confirmed_matches_output") or 0) == 1
        and int(state.get("is_closed") or 0) == 0
    )


def can_false_reopen(state: dict[str, Any] | None) -> bool:
    if not state_is_ready_surface(state):
        return False
    return (
        int(state.get("needs_reopen") or 0) == 1
        and state.get("final_state") == "reopen_auto_confirmed_autofix"
    )


def has_pair_families(row: dict[str, Any]) -> bool:
    families = set(row.get("open_issue_families") or [])
    return {"dynamic_ck3_expression_microagent", "gender_token_microagent"} <= families


def classify_policy(row: dict[str, Any], state: dict[str, Any] | None) -> tuple[str, str, str]:
    text = row["current_text"]
    haystack = " ".join([row["relative_path"], row["key"], text])

    if not state_is_ready_surface(state):
        return "blocked_uncertain", "not_pending_in_segment_state", "not eligible in selected segment-state run"
    if not has_pair_families(row):
        return "blocked_uncertain", "missing_dynamic_gender_pair", "source row does not carry both issue families"
    if not ES_OA_RE.search(text):
        return "blocked_uncertain", "missing_es_oa_token", "no ES_OA surface found"
    if MIXED_ES_RE.search(text):
        return (
            "needs_es_oa_xa_ea_or_ella_deldela_policy",
            "mixed_es_gender_helpers",
            "ES_OA is mixed with another Spanish gender helper",
        )
    if SELECT_RE.search(text):
        return "needs_es_oa_select_cstring_policy", "select_cstring_mixed", "ES_OA appears in a Select_CString context"
    if TITLE_TRAIT_RE.search(haystack):
        return "needs_es_oa_title_trait_policy", "title_trait_or_role", "role/title/trait semantics require a specific policy"
    if RESIDUAL_RE.search(text):
        return "needs_es_oa_residual_repair", "visible_residual_or_agreement", "visible PT-BR residual/agreement issue remains"
    if DOMAIN_RE.search(haystack):
        return "needs_es_oa_domain_context", "domain_context", "domain terminology is needed before deciding gender surface"
    if EVENT_RE.search(haystack):
        return "needs_es_oa_event_context_composer", "event_context", "event prose requires contextual composition"
    if can_false_reopen(state) and not RESIDUAL_RE.search(text) and not DOMAIN_RE.search(haystack) and not EVENT_RE.search(haystack):
        return "es_oa_ready_false_reopen", "false_reopen_clear", "clear ES_OA false reopen candidate for future lifecycle"
    if state_is_ready_surface(state) and not RESIDUAL_RE.search(text) and not DOMAIN_RE.search(haystack) and not EVENT_RE.search(haystack):
        return "es_oa_ready_lifecycle", "ready_lifecycle_clear", "clear ES_OA lifecycle candidate"
    if ES_OA_RE.search(text):
        return "needs_es_oa_target_gender_policy", "target_gender_surface", "target gender agreement needs dedicated ES_OA policy"
    return "needs_new_microagent", "uncategorized_es_oa", "ES_OA route not covered by current subpolicies"


def decide(row: dict[str, Any], state: dict[str, Any] | None) -> dict[str, Any]:
    decision, subpolicy, note = classify_policy(row, state)
    return {
        "segment_id": int(row["segment_id"]),
        "key": row["key"],
        "relative_path": row["relative_path"],
        "current_text": row["current_text"],
        "source_dynamic_gender_decision": row.get("dynamic_gender_decision") or row.get("decision"),
        "es_oa_decision": decision,
        "es_oa_subpolicy": subpolicy,
        "tokens_seen": tokens_seen(row["current_text"]),
        "requires_lifecycle_later": decision in {"es_oa_ready_false_reopen", "es_oa_ready_lifecycle"},
        "requires_apply_later": decision in {
            "es_oa_final_vowel_trim_repair_candidate",
            "es_oa_adjective_agreement_repair_candidate",
        },
        "corrected_text": "",
        "notes": note,
    }


def output_paths() -> tuple[Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_dynamic_gender_es_oa_policy_review"
    return base.with_suffix(".jsonl"), base.with_suffix(".txt")


def write_reports(rows: list[dict[str, Any]], segment_state_run_id: int) -> tuple[Path, Path, Counter[str], Counter[str]]:
    jsonl_path, txt_path = output_paths()
    decision_counts = Counter(row["es_oa_decision"] for row in rows)
    subpolicy_counts = Counter(row["es_oa_subpolicy"] for row in rows)
    ready_count = sum(1 for row in rows if row["es_oa_decision"] in {"es_oa_ready_false_reopen", "es_oa_ready_lifecycle"})
    apply_count = sum(1 for row in rows if row["requires_apply_later"])

    if ready_count >= 5:
        recommendation = "prepare_guarded_es_oa_lifecycle_prompt"
    elif apply_count >= 5:
        recommendation = "prepare_protected_es_oa_apply_prompt"
    elif decision_counts:
        top_decision, top_count = decision_counts.most_common(1)[0]
        recommendation = f"prepare_specific_policy_for_{top_decision}" if top_count >= 10 else "migrate_fragmented_items_to_event_residual_or_global_review"
    else:
        recommendation = "blocked_no_rows"

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Dynamic gender ES_OA policy review",
        "",
        f"segment_state_run_id: {segment_state_run_id}",
        f"total_reviewed: {len(rows)}",
        "",
        "Decision counts:",
    ]
    lines.extend(f"- {key}: {value}" for key, value in sorted(decision_counts.items()))
    lines.extend(["", "Subpolicy counts:"])
    lines.extend(f"- {key}: {value}" for key, value in sorted(subpolicy_counts.items()))
    lines.extend(
        [
            "",
            f"ready_lifecycle_count: {ready_count}",
            f"future_apply_count: {apply_count}",
            f"recommendation: {recommendation}",
            "",
            "Safety: read-only review; no lifecycle, apply, segment-state, confirmations, production, reindex, training, source edits, or output edits.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return jsonl_path, txt_path, decision_counts, subpolicy_counts


def validate_rows(rows: list[dict[str, Any]]) -> None:
    required = {
        "segment_id",
        "key",
        "relative_path",
        "current_text",
        "source_dynamic_gender_decision",
        "es_oa_decision",
        "es_oa_subpolicy",
        "tokens_seen",
        "requires_lifecycle_later",
        "requires_apply_later",
        "corrected_text",
        "notes",
    }
    for row in rows:
        missing = required - set(row)
        if missing:
            raise SystemExit(f"missing fields for {row.get('segment_id')}: {sorted(missing)}")
        if row["source_dynamic_gender_decision"] != "needs_es_oa_policy":
            raise SystemExit(f"unexpected source decision for {row['segment_id']}: {row['source_dynamic_gender_decision']}")
        if row["es_oa_decision"] not in ALLOWED_DECISIONS:
            raise SystemExit(f"unexpected ES_OA decision for {row['segment_id']}: {row['es_oa_decision']}")
        if row["requires_apply_later"] and not row["corrected_text"]:
            raise SystemExit(f"apply candidate without corrected_text: {row['segment_id']}")
        if row["corrected_text"] and TOKEN_RE.findall(row["current_text"]) != TOKEN_RE.findall(row["corrected_text"]):
            raise SystemExit(f"token mismatch in corrected_text: {row['segment_id']}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dynamic-gender-jsonl", required=True)
    parser.add_argument("--segment-state-run-id", type=int, required=True)
    args = parser.parse_args()

    source_rows = collect_source_rows(args.dynamic_gender_jsonl)
    segment_ids = [int(row["segment_id"]) for row in source_rows]
    with connect_readonly() as conn:
        states = fetch_states(conn, args.segment_state_run_id, segment_ids)

    reviewed = [decide(row, states.get(int(row["segment_id"]))) for row in source_rows]
    validate_rows(reviewed)
    jsonl_path, txt_path, decision_counts, subpolicy_counts = write_reports(reviewed, args.segment_state_run_id)

    print(f"wrote_jsonl={jsonl_path}")
    print(f"wrote_txt={txt_path}")
    print(f"segment_state_run_id={args.segment_state_run_id}")
    print(f"total_reviewed={len(reviewed)}")
    print("decision_counts=" + json.dumps(dict(sorted(decision_counts.items())), ensure_ascii=False, sort_keys=True))
    print("subpolicy_counts=" + json.dumps(dict(sorted(subpolicy_counts.items())), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
