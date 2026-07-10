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


SOURCE_DECISIONS = {
    "needs_concept_expression_policy",
    "single_dynamic_ready_concept_expression_false_reopen",
}

TOKEN_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("Concept", re.compile(r"\[[^\]|]+[|][^\]]+\]|\bConcept(?:\(|\b)|\$[^$]*concept[^$]*\$", re.IGNORECASE)),
    ("ScriptValue", re.compile(r"\b(?:ScriptValue|GetScriptValue|script_value)\b", re.IGNORECASE)),
    ("GetTrait", re.compile(r"\b(?:GetTrait|Trait|trait_)[A-Za-z0-9_]*", re.IGNORECASE)),
    ("Custom", re.compile(r"\bCustom(?:\(|Loc|Localization)?\b|custom_localization|custom_loc", re.IGNORECASE)),
    ("Gender", re.compile(r"ES_(?:OA|XA|EA|ElLa|DelDela|A|O)\b|Get(?:SheHe|HerHis|WomanMan|WomenMen)", re.IGNORECASE)),
    ("BracketExpression", re.compile(r"\[[^\]]+\]")),
    ("DollarExpression", re.compile(r"\$[^$]+\$")),
    ("GetExpression", re.compile(r"\bGet[A-Za-z0-9_]*\b", re.IGNORECASE)),
]

GENDER_CUSTOM_RE = re.compile(
    r"ES_(?:OA|XA|EA|ElLa|DelDela|A|O)\b|Get(?:SheHe|HerHis|WomanMan|WomenMen)|"
    r"\bCustom(?:\(|Loc|Localization)?\b|custom_localization|custom_loc",
    re.IGNORECASE,
)
DOMAIN_RE = re.compile(
    r"historical_characters|culture|religion|faith|holy_order|law|succession|title|rank|"
    r"trait_|nickname|dynasty|house|government|factions|ai_personality|great_project|"
    r"coat_of_arms|artifact|buildings|regiment|acclaimed_knight|diarch|realm|war|schemes",
    re.IGNORECASE,
)
EVENT_RE = re.compile(
    r"event|interaction|faction|contract_scheme|major_decisions|bookmark|\.desc$|desc\.|option|toast|"
    r"memory|memories|activity|travel|journey|tourism|scheme",
    re.IGNORECASE,
)
EFFECT_LIST_RE = re.compile(
    r"\$EFFECT_LIST_BULLET\$|effect|modifier|trigger|every_|add_|remove_|set_|gain_|lose_|"
    r"tooltip|_tt$|game_rules|list",
    re.IGNORECASE,
)
REQUIREMENT_RE = re.compile(
    r"required|requirement|requires|condition|eligible|can_|must_|has_|is_|allowed|trigger",
    re.IGNORECASE,
)
SCRIPT_VALUE_RE = re.compile(r"\b(?:ScriptValue|GetScriptValue|script_value)\b", re.IGNORECASE)
GET_TRAIT_RE = re.compile(r"\b(?:GetTrait|Trait|trait_)[A-Za-z0-9_]*", re.IGNORECASE)
RESIDUAL_RE = re.compile(
    r"\b(the|will|must|cannot|should|kingdom|county|duchy|royals|hostile|opinion|"
    r"el|la|los|las|una|uno|verdadero|verdadera|fuerza|probabilidad|mientras)\b",
    re.IGNORECASE,
)
NEW_MICROAGENT_RE = re.compile(
    r"combat|weapon|vassal|stance|claim|casus|belli|prefix|epithet|landed|accolade|knight",
    re.IGNORECASE,
)
WORD_RE = re.compile(r"[\w']+", re.UNICODE)


def as_text(value: Any) -> str:
    return "" if value is None else str(value)


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


def fetch_states(conn: sqlite3.Connection, segment_state_run_id: int, segment_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not segment_ids:
        return {}
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT
            segment_id,
            final_state,
            state_group,
            needs_reopen,
            needs_output_apply,
            confirmed_matches_output
        FROM segment_state_items
        WHERE run_id = ?
          AND segment_id IN ({placeholders})
        """,
        (segment_state_run_id, *segment_ids),
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def fetch_open_family_shapes(conn: sqlite3.Connection, ledger_run_id: int, segment_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not segment_ids:
        return {}
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT
            segment_id,
            COUNT(*) AS open_issue_count,
            SUM(CASE WHEN issue_family = 'autofix_unknown_microagent' THEN 1 ELSE 0 END) AS autofix_count,
            SUM(CASE WHEN issue_family != 'autofix_unknown_microagent' THEN 1 ELSE 0 END) AS other_family_count
        FROM ml_issue_ledger_items
        WHERE run_id = ?
          AND status = 'open'
          AND segment_id IN ({placeholders})
        GROUP BY segment_id
        """,
        (ledger_run_id, *segment_ids),
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def word_count(text: str) -> int:
    cleaned = re.sub(r"\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_:.{};,|]+|#!", " ", text)
    return len(WORD_RE.findall(cleaned))


def tokens_seen(text: str) -> list[str]:
    seen: list[str] = []
    for label, pattern in TOKEN_PATTERNS:
        if pattern.search(text):
            seen.append(label)
    return seen


def state_and_family_clean(
    state: dict[str, Any] | None,
    family_shape: dict[str, Any] | None,
    *,
    false_reopen: bool,
) -> bool:
    clean = bool(
        state
        and family_shape
        and state.get("state_group") == "pending"
        and int(state.get("needs_output_apply") or 0) == 0
        and int(state.get("confirmed_matches_output") or 0) == 1
        and int(family_shape.get("open_issue_count") or 0) == 1
        and int(family_shape.get("autofix_count") or 0) == 1
        and int(family_shape.get("other_family_count") or 0) == 0
    )
    if false_reopen:
        clean = bool(
            clean
            and state
            and state.get("final_state") == "reopen_auto_confirmed_autofix"
            and int(state.get("needs_reopen") or 0) == 1
        )
    return clean


def decide(row: dict[str, Any], state: dict[str, Any] | None, family_shape: dict[str, Any] | None) -> dict[str, Any]:
    text = as_text(row.get("current_text"))
    haystack = " ".join([as_text(row.get("relative_path")), as_text(row.get("key")), text])
    seen = tokens_seen(text)
    wc = word_count(text)
    source_decision = as_text(row.get("dynamic_decision"))
    false_reopen_source = source_decision == "single_dynamic_ready_concept_expression_false_reopen"
    clean_state = state_and_family_clean(state, family_shape, false_reopen=false_reopen_source)
    no_ready_blockers = (
        text.count("[") == text.count("]")
        and text.count("$") % 2 == 0
        and not RESIDUAL_RE.search(text)
        and not DOMAIN_RE.search(haystack)
        and not GENDER_CUSTOM_RE.search(haystack)
    )
    has_multiline = "\n" in text or "\\n" in text

    if false_reopen_source and clean_state and no_ready_blockers:
        decision = "single_concept_ready_false_reopen"
        subpolicy = "single_concept_false_reopen_aligned"
    elif GENDER_CUSTOM_RE.search(haystack):
        decision = "needs_single_concept_custom_loc_or_gender_policy"
        subpolicy = "single_concept_with_custom_loc_or_gender"
    elif SCRIPT_VALUE_RE.search(haystack):
        decision = "needs_single_concept_script_value_policy"
        subpolicy = "single_concept_with_script_value"
    elif GET_TRAIT_RE.search(haystack):
        decision = "needs_single_concept_get_trait_policy"
        subpolicy = "single_concept_with_get_trait"
    elif DOMAIN_RE.search(haystack):
        decision = "needs_single_concept_domain_policy"
        subpolicy = "single_concept_with_domain_sensitive_surface"
    elif EFFECT_LIST_RE.search(haystack) or has_multiline:
        decision = "needs_single_concept_effect_list_policy"
        subpolicy = "single_concept_with_effect_list"
    elif REQUIREMENT_RE.search(haystack):
        decision = "needs_single_concept_requirement_policy"
        subpolicy = "single_concept_with_requirement_condition"
    elif EVENT_RE.search(haystack) or wc >= 12:
        decision = "needs_event_context_composer"
        subpolicy = "single_concept_with_event_or_long_context"
    elif RESIDUAL_RE.search(text):
        decision = "needs_residual_repair"
        subpolicy = "single_concept_with_visible_residual"
    elif clean_state and no_ready_blockers and wc <= 8:
        decision = "single_concept_ready_simple_expression_lifecycle"
        subpolicy = "single_simple_concept_expression"
    elif clean_state and no_ready_blockers:
        decision = "single_concept_ready_tooltip_expression_lifecycle"
        subpolicy = "single_tooltip_or_label_concept_expression"
    elif NEW_MICROAGENT_RE.search(haystack):
        decision = "needs_new_microagent"
        subpolicy = "single_concept_with_new_surface"
    else:
        decision = "blocked_uncertain"
        subpolicy = "single_unclassified_concept_expression"

    requires_lifecycle = decision.startswith("single_concept_ready_")
    return {
        "concept_decision": decision,
        "concept_subpolicy": subpolicy,
        "tokens_seen": seen,
        "requires_lifecycle_later": requires_lifecycle,
        "requires_apply_later": False,
        "notes": (
            "single-family concept expression is aligned and suitable for a future narrow lifecycle"
            if requires_lifecycle
            else f"route to {decision} before lifecycle closure"
        ),
    }


def output_paths() -> tuple[Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_autofix_unknown_single_concept_expression_policy_review"
    return base.with_suffix(".jsonl"), base.with_suffix(".txt")


def write_reports(rows: list[dict[str, Any]]) -> tuple[Path, Path, Counter[str], Counter[str]]:
    jsonl_path, txt_path = output_paths()
    decision_counts = Counter(row["concept_decision"] for row in rows)
    subpolicy_counts = Counter(row["concept_subpolicy"] for row in rows)
    ready_total = sum(count for key, count in decision_counts.items() if key.startswith("single_concept_ready_"))
    dominant = subpolicy_counts.most_common(1)[0] if subpolicy_counts else ("none", 0)
    if ready_total >= 30:
        recommendation = "prepare_narrow_single_concept_lifecycle"
    elif dominant[1] >= 50 and dominant[0].startswith("single_concept_with_"):
        recommendation = f"prepare_specific_policy:{dominant[0]}"
    else:
        recommendation = "migrate_to_effect_list_or_multiline_dynamic_or_residual"

    jsonl_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )
    lines = [
        "Autofix unknown single-family concept expression policy review",
        f"reviewed: {len(rows):,}",
        "",
        "Decision counts:",
    ]
    if decision_counts:
        for key, count in decision_counts.most_common():
            lines.append(f"- {key}: {count:,}")
    else:
        lines.append("- none: 0")
    lines.extend(["", "Top technical subpolicies:"])
    if subpolicy_counts:
        for key, count in subpolicy_counts.most_common():
            lines.append(f"- {key}: {count:,}")
    else:
        lines.append("- none: 0")
    lines.extend(["", f"single_concept_ready_total: {ready_total:,}", f"Recommendation: {recommendation}"])
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return jsonl_path, txt_path, decision_counts, subpolicy_counts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dynamic-jsonl", required=True)
    parser.add_argument("--segment-state-run-id", type=int, required=True)
    parser.add_argument("--ledger-run-id", type=int, default=76)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    dynamic_rows = [
        row
        for row in read_jsonl(db.project_path(args.dynamic_jsonl))
        if row.get("dynamic_subpolicy") == "concept_expression"
        and row.get("dynamic_decision") in SOURCE_DECISIONS
    ]
    segment_ids = [int(row["segment_id"]) for row in dynamic_rows]
    with connect_readonly() as conn:
        states = fetch_states(conn, args.segment_state_run_id, segment_ids)
        family_shapes = fetch_open_family_shapes(conn, args.ledger_run_id, segment_ids)
    reviewed: list[dict[str, Any]] = []
    for row in dynamic_rows:
        segment_id = int(row["segment_id"])
        concept = decide(row, states.get(segment_id), family_shapes.get(segment_id))
        reviewed.append(
            {
                "segment_id": segment_id,
                "key": row["key"],
                "relative_path": row["relative_path"],
                "current_text": row["current_text"],
                "source_dynamic_decision": row["dynamic_decision"],
                **concept,
            }
        )
    jsonl_path, txt_path, decision_counts, subpolicy_counts = write_reports(reviewed)
    ready_total = sum(count for key, count in decision_counts.items() if key.startswith("single_concept_ready_"))
    print(f"reviewed={len(reviewed)}")
    print(f"single_concept_ready={ready_total}")
    print(f"jsonl_report={jsonl_path}")
    print(f"txt_report={txt_path}")
    print("decision_counts=" + json.dumps(dict(decision_counts), ensure_ascii=False, sort_keys=True))
    print("top_subpolicies=" + json.dumps(dict(subpolicy_counts.most_common(10)), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
