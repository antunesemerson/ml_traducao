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


TARGET_FAMILY = "autofix_unknown_microagent"

ALLOWED_DECISIONS = {
    "single_dynamic_ready_false_reopen",
    "single_dynamic_ready_lifecycle",
    "needs_single_dynamic_concept_expression_policy",
    "needs_single_dynamic_select_cstring_policy",
    "needs_single_dynamic_custom_loc_policy",
    "needs_single_dynamic_gender_helper_policy",
    "needs_single_dynamic_scope_getter_policy",
    "needs_single_dynamic_script_value_policy",
    "needs_single_dynamic_effect_list_policy",
    "needs_single_dynamic_requirement_tooltip_policy",
    "needs_single_dynamic_event_context_composer",
    "needs_single_dynamic_domain_context",
    "needs_single_dynamic_residual_repair",
    "needs_new_microagent",
    "blocked_uncertain",
}

TOKEN_RE = re.compile(
    r"Select_CString|Custom\(|ES_(?:OA|XA|EA|ElLa|DelDela|AlAla)|\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_:.{};,|]+|#!",
    re.IGNORECASE,
)
CONCEPT_RE = re.compile(r"\[[A-Za-z0-9_]+(?:\|[^\]]+)?\]|\$[A-Za-z0-9_.:-]+\$|Concept\s*\(", re.IGNORECASE)
SELECT_RE = re.compile(r"Select_CString|SelectLocalization|SelectLocalizationIf", re.IGNORECASE)
CUSTOM_RE = re.compile(r"Custom\(|GetCustom|custom_loc", re.IGNORECASE)
GENDER_RE = re.compile(r"ES_(?:OA|XA|EA|ElLa|DelDela|AlAla)\b|Get(?:SheHe|HerHis|HerHim|WomanMan|WomenMen)", re.IGNORECASE)
SCOPE_GETTER_RE = re.compile(r"\[[^\]]*\.(?:Get|Is|Has)[A-Za-z0-9_]*[^\]]*\]|\b(?:ROOT|CHARACTER|TARGET|SCOPE|THIS)\.", re.IGNORECASE)
SCRIPT_VALUE_RE = re.compile(r"ScriptValue|GetScriptValue|\|V[0-9]?|[0-9]+\s*%|#P\s*[0-9]", re.IGNORECASE)
EFFECT_LIST_RE = re.compile(r"\n|^[-*]\s|#indent|#weak|#bold|\n\s*#", re.IGNORECASE)
TOOLTIP_RE = re.compile(r"#tooltip|_tt$|tooltip|trigger|requirement|required|available|NO_CHANCE|CHANCE|WILL_GET", re.IGNORECASE)
EVENT_RE = re.compile(r"event|\.desc|desc\.|option|toast|interaction|scheme|memory|activity|travel|journey|contract|request", re.IGNORECASE)
DOMAIN_RE = re.compile(
    r"mandala|seal_of_investiture|domicile|estate|paiza|court|children|heirs|knights|prowess|piety|"
    r"culture|religion|faith|holy_order|law|succession|title|trait|artifact|building|regiment|government|contract",
    re.IGNORECASE,
)
RESIDUAL_RE = re.compile(
    r"\b(?:Pres[eé]ntate|decisi[oó]n|seg[uú]n|conceder[aá]|educados|Probabilidad|menos probable|personajes|situados|tus|tu|una|uno|el|la|los|las)\b",
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
        if row.get("single_autofix_decision") != "needs_dynamic_expression_agent":
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


def fetch_family_counts(conn: sqlite3.Connection, ledger_run_id: int, segment_ids: list[int]) -> dict[int, tuple[int, int, int]]:
    if not segment_ids:
        return {}
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT segment_id,
               COUNT(*) AS open_count,
               SUM(CASE WHEN issue_family = ? THEN 1 ELSE 0 END) AS target_count,
               SUM(CASE WHEN issue_family != ? THEN 1 ELSE 0 END) AS other_count
        FROM ml_issue_ledger_items
        WHERE run_id = ?
          AND status = 'open'
          AND segment_id IN ({placeholders})
        GROUP BY segment_id
        """,
        (TARGET_FAMILY, TARGET_FAMILY, ledger_run_id, *segment_ids),
    ).fetchall()
    return {
        int(row["segment_id"]): (int(row["open_count"]), int(row["target_count"] or 0), int(row["other_count"] or 0))
        for row in rows
    }


def tokens_seen(text: str) -> list[str]:
    labels: list[str] = []
    for label, pattern in [
        ("Concept", CONCEPT_RE),
        ("Select_CString", SELECT_RE),
        ("CustomLoc", CUSTOM_RE),
        ("GenderHelper", GENDER_RE),
        ("ScopeGetter", SCOPE_GETTER_RE),
        ("ScriptValue", SCRIPT_VALUE_RE),
        ("Tooltip", TOOLTIP_RE),
        ("CK3DynamicToken", re.compile(r"\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_:.{};,|]+|#!")),
    ]:
        if pattern.search(text):
            labels.append(label)
    return labels


def state_is_pending_confirmed(state: dict[str, Any] | None) -> bool:
    if not state:
        return False
    return (
        state.get("state_group") == "pending"
        and int(state.get("needs_output_apply") or 0) == 0
        and int(state.get("confirmed_matches_output") or 0) == 1
        and int(state.get("is_closed") or 0) == 0
    )


def family_is_exact(family_counts: tuple[int, int, int] | None) -> bool:
    return family_counts == (1, 1, 0)


def can_false_reopen(state: dict[str, Any] | None) -> bool:
    if not state_is_pending_confirmed(state):
        return False
    return (
        int(state.get("needs_reopen") or 0) == 1
        and state.get("final_state") == "reopen_auto_confirmed_autofix"
    )


def classify(row: dict[str, Any], state: dict[str, Any] | None, family_counts: tuple[int, int, int] | None) -> tuple[str, str, str]:
    text = row["current_text"]
    key = row["key"]
    haystack = " ".join([row["relative_path"], key, text])

    if row.get("single_autofix_decision") != "needs_dynamic_expression_agent":
        return "blocked_uncertain", "unexpected_source_branch", "source row is not needs_dynamic_expression_agent"
    if not state_is_pending_confirmed(state):
        return "blocked_uncertain", "not_pending_in_segment_state", "not eligible in selected segment-state run"
    if not family_is_exact(family_counts):
        return "blocked_uncertain", "not_single_autofix_family", "ledger no longer has exactly one autofix_unknown open family"
    if text.count("[") != text.count("]") or text.count("$") % 2 != 0:
        return "needs_single_dynamic_residual_repair", "broken_dynamic_token_boundary", "dynamic token boundary looks malformed"
    if GENDER_RE.search(text):
        return "needs_single_dynamic_gender_helper_policy", "gender_helper", "gender helper requires a dedicated policy"
    if SELECT_RE.search(text):
        return "needs_single_dynamic_select_cstring_policy", "select_cstring", "Select_CString-like expression requires a dedicated policy"
    if CUSTOM_RE.search(text):
        return "needs_single_dynamic_custom_loc_policy", "custom_loc", "Custom localization helper requires a dedicated policy"
    if TOOLTIP_RE.search(haystack):
        return "needs_single_dynamic_requirement_tooltip_policy", "requirement_tooltip", "requirement or tooltip dynamic surface needs policy"
    if SCRIPT_VALUE_RE.search(text):
        return "needs_single_dynamic_script_value_policy", "script_value_or_numeric", "numeric/script value dynamic expression needs policy"
    if EFFECT_LIST_RE.search(text):
        return "needs_single_dynamic_effect_list_policy", "effect_list_or_multiline", "multiline/effect-list surface needs policy"
    if DOMAIN_RE.search(haystack):
        return "needs_single_dynamic_domain_context", "domain_sensitive_dynamic", "domain-sensitive dynamic concept needs context"
    if CONCEPT_RE.search(text):
        return "needs_single_dynamic_concept_expression_policy", "concept_expression", "concept link or game concept expression needs policy"
    if SCOPE_GETTER_RE.search(text):
        return "needs_single_dynamic_scope_getter_policy", "scope_getter", "scope/getter dynamic expression needs policy"
    if RESIDUAL_RE.search(text):
        return "needs_single_dynamic_residual_repair", "visible_residual", "visible Spanish/English/PT-BR residual remains"
    if EVENT_RE.search(haystack) or len(text) > 160:
        return "needs_single_dynamic_event_context_composer", "event_context_dynamic", "event or longform dynamic text needs context"
    if can_false_reopen(state):
        return "single_dynamic_ready_false_reopen", "ready_false_reopen_clear", "dynamic surface appears preserved and false-reopen governable"
    return "single_dynamic_ready_lifecycle", "ready_lifecycle_clear", "dynamic surface appears preserved for future lifecycle"


def decide(row: dict[str, Any], state: dict[str, Any] | None, family_counts: tuple[int, int, int] | None) -> dict[str, Any]:
    decision, subpolicy, notes = classify(row, state, family_counts)
    return {
        "segment_id": int(row["segment_id"]),
        "key": row["key"],
        "relative_path": row["relative_path"],
        "current_text": row["current_text"],
        "source_single_autofix_decision": row["single_autofix_decision"],
        "dynamic_decision": decision,
        "dynamic_subpolicy": subpolicy,
        "tokens_seen": tokens_seen(row["current_text"]),
        "requires_lifecycle_later": decision in {"single_dynamic_ready_false_reopen", "single_dynamic_ready_lifecycle"},
        "requires_apply_later": False,
        "corrected_text": "",
        "notes": notes,
    }


def output_paths() -> tuple[Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_autofix_unknown_single_dynamic_batch2_sublane_review"
    return base.with_suffix(".jsonl"), base.with_suffix(".txt")


def write_reports(rows: list[dict[str, Any]]) -> tuple[Path, Path, Counter[str], Counter[str]]:
    jsonl_path, txt_path = output_paths()
    decision_counts = Counter(row["dynamic_decision"] for row in rows)
    subpolicy_counts = Counter(row["dynamic_subpolicy"] for row in rows)
    ready_count = sum(1 for row in rows if row["requires_lifecycle_later"])
    apply_count = sum(1 for row in rows if row["requires_apply_later"])

    if ready_count >= 30:
        recommendation = "prepare_single_dynamic_readonly_lifecycle"
    elif decision_counts["needs_single_dynamic_residual_repair"] >= 5:
        recommendation = "prepare_single_dynamic_residual_split_before_apply"
    else:
        needs_counts = Counter({key: value for key, value in decision_counts.items() if key.startswith("needs_single_dynamic_")})
        if needs_counts and needs_counts.most_common(1)[0][1] >= 50:
            recommendation = f"prepare_specific_policy_or_microagent_for_{needs_counts.most_common(1)[0][0]}"
        else:
            recommendation = "return_to_global_or_migrate_to_batch2_event_context"

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Autofix unknown single-family dynamic batch2 sublane review",
        "",
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
        "source_single_autofix_decision",
        "dynamic_decision",
        "dynamic_subpolicy",
        "tokens_seen",
        "requires_lifecycle_later",
        "requires_apply_later",
        "corrected_text",
        "notes",
    }
    seen: set[int] = set()
    for row in rows:
        missing = required - set(row)
        if missing:
            raise SystemExit(f"missing fields for {row.get('segment_id')}: {sorted(missing)}")
        if row["segment_id"] in seen:
            raise SystemExit(f"duplicate segment_id: {row['segment_id']}")
        seen.add(row["segment_id"])
        if row["source_single_autofix_decision"] != "needs_dynamic_expression_agent":
            raise SystemExit(f"unexpected source decision for {row['segment_id']}: {row['source_single_autofix_decision']}")
        if row["dynamic_decision"] not in ALLOWED_DECISIONS:
            raise SystemExit(f"unexpected dynamic decision for {row['segment_id']}: {row['dynamic_decision']}")
        if row["requires_apply_later"] and not row["corrected_text"]:
            raise SystemExit(f"apply candidate without corrected_text: {row['segment_id']}")
        if row["corrected_text"] and TOKEN_RE.findall(row["current_text"]) != TOKEN_RE.findall(row["corrected_text"]):
            raise SystemExit(f"token mismatch in corrected_text: {row['segment_id']}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--single-family-jsonl", required=True)
    parser.add_argument("--segment-state-run-id", type=int, required=True)
    args = parser.parse_args()

    source_rows = collect_source_rows(args.single_family_jsonl)
    segment_ids = [int(row["segment_id"]) for row in source_rows]
    with connect_readonly() as conn:
        states = fetch_states(conn, args.segment_state_run_id, segment_ids)
        family_counts = fetch_family_counts(conn, 76, segment_ids)

    reviewed = [
        decide(row, states.get(int(row["segment_id"])), family_counts.get(int(row["segment_id"])))
        for row in source_rows
    ]
    validate_rows(reviewed)
    jsonl_path, txt_path, decision_counts, subpolicy_counts = write_reports(reviewed)

    print(f"wrote_jsonl={jsonl_path}")
    print(f"wrote_txt={txt_path}")
    print(f"total_reviewed={len(reviewed)}")
    print("decision_counts=" + json.dumps(dict(sorted(decision_counts.items())), ensure_ascii=False, sort_keys=True))
    print("subpolicy_counts=" + json.dumps(dict(sorted(subpolicy_counts.items())), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
