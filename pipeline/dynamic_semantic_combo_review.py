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


TARGET_FAMILIES = ("dynamic_ck3_expression_microagent", "semantic_review_router")

ALLOWED_DECISIONS = {
    "dynamic_semantic_ready_false_reopen",
    "dynamic_semantic_ready_lifecycle",
    "needs_dynamic_semantic_concept_expression_policy",
    "needs_dynamic_semantic_select_cstring_policy",
    "needs_dynamic_semantic_custom_loc_policy",
    "needs_dynamic_semantic_scope_getter_policy",
    "needs_dynamic_semantic_script_value_policy",
    "needs_dynamic_semantic_requirement_tooltip_policy",
    "needs_dynamic_semantic_effect_list_policy",
    "needs_dynamic_semantic_domain_context",
    "needs_dynamic_semantic_event_context_composer",
    "needs_dynamic_semantic_plain_prose_context_composer",
    "needs_dynamic_semantic_residual_repair",
    "needs_dynamic_semantic_gender_or_custom_loc_policy",
    "needs_new_microagent",
    "blocked_uncertain",
}

CK3_TOKEN_RE = re.compile(
    r"Select_CString|Custom\(|\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_:.{};,|]+|#!|@[A-Za-z0-9_]+!",
    re.IGNORECASE,
)
CONCEPT_RE = re.compile(r"\[[A-Za-z0-9_]+(?:\|[^\]]+)?\]|\$game_concept[^$]*\$|Concept\s*\(", re.IGNORECASE)
SELECT_CSTRING_RE = re.compile(r"Select_CString|SelectLocalization|SelectLocalizationIf", re.IGNORECASE)
CUSTOM_LOC_RE = re.compile(r"Custom\(|custom_loc|GetCustom|Window\.Get|GetDesignateTooltip", re.IGNORECASE)
GENDER_RE = re.compile(
    r"ES_(?:OA|XA|EA|ElLa|DelDela|AlAla|A|O)\b|Get(?:SheHe|HerHis|HerHim|WomanMan|WomenMen)",
    re.IGNORECASE,
)
SCOPE_GETTER_RE = re.compile(
    r"\[[^\]]*(?:ROOT|CHARACTER|TARGET|SCOPE|THIS|actor|recipient|target|root)[^\]]*\]|"
    r"\[[^\]]*\.(?:Get|Is|Has)[A-Za-z0-9_]*[^\]]*\]|Get[A-Za-z0-9_]+\(",
    re.IGNORECASE,
)
SCRIPT_VALUE_RE = re.compile(r"ScriptValue|GetScriptValue|script_value|\|V[0-9]?|#P\s*[0-9]|\b[0-9]+\s*%", re.IGNORECASE)
TOOLTIP_RE = re.compile(
    r"#tooltip|tooltip|_tt\b|_tt$|trigger|requirement|required|available|can_|cannot|"
    r"NO_CHANCE|CHANCE|WILL_GET|valid_|invalid_",
    re.IGNORECASE,
)
EFFECT_LIST_RE = re.compile(r"\\n|\n|\$EFFECT_LIST_BULLET\$|^[-*]\s|#indent|#weak|#bold|#low|#high|effect|effects_l_", re.IGNORECASE)
DOMAIN_RE = re.compile(
    r"religion|faith|culture|title|law|succession|artifact|activity|trait|government|governor|"
    r"county|duchy|kingdom|empire|realm|house|dynasty|nickname|accolade|knight|court|"
    r"building|holding|contract|travel|tournament|scheme|memory|legend|holy_order|regiment|"
    r"tax|vassal|liege|claim|war|battle|prowess|piety|prestige|gold|herd|place|location",
    re.IGNORECASE,
)
EVENT_RE = re.compile(
    r"event|\.desc|desc\.|option|toast|dialogue|story|memory|memories|activity|travel|journey|"
    r"interaction|letter|request|petition|scheme|outcome|ongoing|flavor|narrative|episode",
    re.IGNORECASE,
)
RESIDUAL_RE = re.compile(
    r"NÃ|Ãƒ|Â|�|\b(?:aumenta|consiguio|consiguió|ganaste|ganar|tendras|tendrás|lograste|acepta|"
    r"posesion|posesión|azar|conceder|reclamacion|reclamación|sera|será|mas|más|facil|fácil|"
    r"revela|falsedad|ensenar|enseñar|provecho|poder|desheredar|atacante|numero|número|"
    r"elegir|enfoque|sufre|corte|bajar|perdiste|duelo|pres[eé]ntate|cambiar|"
    r"the|your|you|their|has|have|will|can|cannot)\b",
    re.IGNORECASE,
)


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_run(conn: sqlite3.Connection, table: str, run_id: int) -> dict[str, Any]:
    row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise SystemExit(f"run not found: {table}.id={run_id}")
    result = dict(row)
    if "finished_at" in result and not result["finished_at"]:
        raise SystemExit(f"run is not finalized: {table}.id={run_id}")
    return result


def fetch_universe(conn: sqlite3.Connection, segment_state_run_id: int, ledger_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        WITH open_family_shape AS (
            SELECT
                segment_id,
                COUNT(DISTINCT issue_family) AS family_count,
                SUM(CASE WHEN issue_family = 'dynamic_ck3_expression_microagent' THEN 1 ELSE 0 END) AS dynamic_count,
                SUM(CASE WHEN issue_family = 'semantic_review_router' THEN 1 ELSE 0 END) AS semantic_count,
                SUM(CASE WHEN issue_family NOT IN (
                    'dynamic_ck3_expression_microagent',
                    'semantic_review_router'
                ) THEN 1 ELSE 0 END) AS other_count
            FROM ml_issue_ledger_items
            WHERE run_id = ?
              AND status = 'open'
            GROUP BY segment_id
        )
        SELECT
            s.segment_id,
            s.relative_path,
            s.source_key AS key,
            s.source_line_number,
            s.final_state,
            s.state_group,
            s.needs_output_apply,
            s.confirmed_matches_output,
            s.needs_reopen,
            s.is_closed,
            s.priority_score,
            out.portuguese_text AS current_text,
            src.spanish_text,
            src.english_text
        FROM segment_state_items s
        JOIN open_family_shape shape
          ON shape.segment_id = s.segment_id
        LEFT JOIN output_segments out
          ON out.segment_id = s.segment_id
        LEFT JOIN source_segments src
          ON src.id = s.segment_id
        WHERE s.run_id = ?
          AND s.state_group = 'pending'
          AND COALESCE(s.is_closed, 0) = 0
          AND COALESCE(s.needs_output_apply, 0) = 0
          AND COALESCE(s.confirmed_matches_output, 0) = 1
          AND shape.family_count = 2
          AND shape.dynamic_count > 0
          AND shape.semantic_count > 0
          AND shape.other_count = 0
        ORDER BY s.priority_score DESC, s.segment_id
        """,
        (ledger_run_id, segment_state_run_id),
    ).fetchall()
    return [dict(row) for row in rows]


def tokens_seen(text: str) -> list[str]:
    labels: list[str] = []
    checks = [
        ("Select_CString", SELECT_CSTRING_RE),
        ("CustomLoc", CUSTOM_LOC_RE),
        ("GenderHelper", GENDER_RE),
        ("Concept", CONCEPT_RE),
        ("ScopeGetter", SCOPE_GETTER_RE),
        ("ScriptValue", SCRIPT_VALUE_RE),
        ("Tooltip", TOOLTIP_RE),
        ("EffectList", EFFECT_LIST_RE),
    ]
    for label, pattern in checks:
        if pattern.search(text) and label not in labels:
            labels.append(label)
    if CK3_TOKEN_RE.search(text) and "CK3DynamicToken" not in labels:
        labels.append("CK3DynamicToken")
    return labels


def token_boundaries_ok(text: str) -> bool:
    return text.count("[") == text.count("]") and text.count("$") % 2 == 0


def classify(row: dict[str, Any]) -> tuple[str, str, str]:
    current_text = str(row.get("current_text") or "")
    haystack = " ".join(
        str(row.get(key) or "")
        for key in ("relative_path", "key", "current_text", "spanish_text", "english_text")
    )
    key_path = " ".join(str(row.get(key) or "") for key in ("relative_path", "key"))

    if not current_text.strip():
        return "blocked_uncertain", "missing_current_text", "no output text was available for review"
    if not token_boundaries_ok(current_text):
        return "needs_dynamic_semantic_residual_repair", "broken_dynamic_token_boundary", "dynamic token boundary looks malformed"
    if RESIDUAL_RE.search(current_text):
        return "needs_dynamic_semantic_residual_repair", "visible_residual", "visible Spanish/English residual or rough text remains"
    if GENDER_RE.search(haystack):
        return "needs_dynamic_semantic_gender_or_custom_loc_policy", "gender_or_custom_loc", "gender/custom-loc helper needs guarded policy"
    if SELECT_CSTRING_RE.search(current_text):
        return "needs_dynamic_semantic_select_cstring_policy", "select_cstring", "Select_CString-like expression needs dedicated policy"
    if CUSTOM_LOC_RE.search(current_text):
        return "needs_dynamic_semantic_custom_loc_policy", "custom_loc", "Custom localization helper needs dedicated policy"
    if TOOLTIP_RE.search(key_path):
        return "needs_dynamic_semantic_requirement_tooltip_policy", "requirement_tooltip", "requirement or tooltip surface needs policy"
    if SCRIPT_VALUE_RE.search(haystack):
        return "needs_dynamic_semantic_script_value_policy", "script_value", "script value or numeric expression needs policy"
    if EFFECT_LIST_RE.search(current_text):
        return "needs_dynamic_semantic_effect_list_policy", "effect_list", "effect list, multiline, or formatted block needs policy"
    if DOMAIN_RE.search(haystack):
        return "needs_dynamic_semantic_domain_context", "domain_context", "domain-sensitive semantic text needs context"
    if CONCEPT_RE.search(current_text):
        return "needs_dynamic_semantic_concept_expression_policy", "concept_expression", "concept expression needs policy"
    if SCOPE_GETTER_RE.search(current_text):
        return "needs_dynamic_semantic_scope_getter_policy", "scope_getter", "scope/getter expression needs policy"
    if EVENT_RE.search(haystack) or len(current_text) > 160:
        return "needs_dynamic_semantic_event_context_composer", "event_context", "event/contextual or longform dynamic text needs composer"
    if CK3_TOKEN_RE.search(current_text):
        return "needs_new_microagent", "unclassified_dynamic_semantic_token", "dynamic semantic token did not fit a known safe policy"
    if len(current_text) > 70:
        return "needs_dynamic_semantic_plain_prose_context_composer", "plain_prose_context", "plain prose needs semantic context review"
    if int(row.get("needs_reopen") or 0) == 1 and row.get("final_state") == "reopen_auto_confirmed_autofix":
        return "dynamic_semantic_ready_false_reopen", "ready_false_reopen", "plain dynamic text appears aligned for future false-reopen lifecycle"
    return "dynamic_semantic_ready_lifecycle", "ready_lifecycle", "plain dynamic text appears aligned for future lifecycle"


def decide(row: dict[str, Any]) -> dict[str, Any]:
    decision, subpolicy, notes = classify(row)
    return {
        "segment_id": int(row["segment_id"]),
        "key": row["key"],
        "relative_path": row["relative_path"],
        "source_line_number": row["source_line_number"],
        "current_text": row.get("current_text") or "",
        "open_issue_families": list(TARGET_FAMILIES),
        "dynamic_semantic_decision": decision,
        "dynamic_semantic_subpolicy": subpolicy,
        "tokens_seen": tokens_seen(row.get("current_text") or ""),
        "requires_lifecycle_later": decision in {
            "dynamic_semantic_ready_false_reopen",
            "dynamic_semantic_ready_lifecycle",
        },
        "requires_apply_later": False,
        "corrected_text": "",
        "notes": notes,
    }


def output_paths() -> tuple[Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_dynamic_semantic_combo_review"
    return base.with_suffix(".jsonl"), base.with_suffix(".txt")


def recommendation(decision_counts: Counter[str]) -> str:
    ready_count = decision_counts["dynamic_semantic_ready_false_reopen"] + decision_counts["dynamic_semantic_ready_lifecycle"]
    if ready_count >= 50:
        return "prepare_readonly_lifecycle_for_dynamic_semantic_ready"
    needs_counts = Counter({key: value for key, value in decision_counts.items() if key.startswith("needs_dynamic_semantic_")})
    if needs_counts:
        top_decision, top_count = needs_counts.most_common(1)[0]
        if top_count >= 50:
            return f"prepare_specific_policy_microagent_for_{top_decision}"
    if decision_counts["needs_dynamic_semantic_residual_repair"] >= 5:
        return "prepare_residual_split_before_any_apply"
    if decision_counts["needs_dynamic_semantic_event_context_composer"] >= 30:
        return "prepare_event_context_split"
    return "fragmented_migrate_to_culture_semantic_dynamic_or_global_diagnostic"


def write_reports(
    rows: list[dict[str, Any]],
    *,
    segment_state_run_id: int,
    ledger_run_id: int,
    universe_count: int,
) -> tuple[Path, Path, Counter[str], Counter[str]]:
    jsonl_path, txt_path = output_paths()
    decision_counts = Counter(row["dynamic_semantic_decision"] for row in rows)
    subpolicy_counts = Counter(row["dynamic_semantic_subpolicy"] for row in rows)
    ready_count = sum(1 for row in rows if row["requires_lifecycle_later"])
    apply_count = sum(1 for row in rows if row["requires_apply_later"])
    rec = recommendation(decision_counts)

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Dynamic + semantic exact combo review",
        "",
        f"segment_state_run_id: {segment_state_run_id}",
        f"ledger_run_id: {ledger_run_id}",
        f"eligible_exact_combo_universe: {universe_count}",
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
            f"ready_for_future_lifecycle: {ready_count}",
            f"future_apply_candidates: {apply_count}",
            f"recommendation: {rec}",
            "",
            "Safety: read-only review; no lifecycle, apply, segment-state, confirmations, production, reindex, training, source edits, or output edits.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return jsonl_path, txt_path, decision_counts, subpolicy_counts


def validate_review_rows(rows: list[dict[str, Any]], limit: int) -> None:
    required = {
        "segment_id",
        "key",
        "relative_path",
        "current_text",
        "open_issue_families",
        "dynamic_semantic_decision",
        "dynamic_semantic_subpolicy",
        "tokens_seen",
        "requires_lifecycle_later",
        "requires_apply_later",
        "corrected_text",
        "notes",
    }
    if len(rows) > limit:
        raise SystemExit(f"review exceeded limit: {len(rows)} > {limit}")
    seen: set[int] = set()
    for row in rows:
        missing = required - set(row)
        if missing:
            raise SystemExit(f"missing fields for {row.get('segment_id')}: {sorted(missing)}")
        segment_id = int(row["segment_id"])
        if segment_id in seen:
            raise SystemExit(f"duplicate segment_id: {segment_id}")
        seen.add(segment_id)
        if row["open_issue_families"] != list(TARGET_FAMILIES):
            raise SystemExit(f"unexpected family shape for {segment_id}: {row['open_issue_families']}")
        if row["dynamic_semantic_decision"] not in ALLOWED_DECISIONS:
            raise SystemExit(f"unexpected decision for {segment_id}: {row['dynamic_semantic_decision']}")
        if row["requires_apply_later"] and not row["corrected_text"]:
            raise SystemExit(f"apply candidate without corrected_text: {segment_id}")
        if row["corrected_text"]:
            before = CK3_TOKEN_RE.findall(row["current_text"])
            after = CK3_TOKEN_RE.findall(row["corrected_text"])
            if before != after:
                raise SystemExit(f"CK3 token mismatch in corrected_text: {segment_id}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--segment-state-run-id", type=int, required=True)
    parser.add_argument("--ledger-run-id", type=int, required=True)
    parser.add_argument("--limit", type=int, default=240)
    args = parser.parse_args()

    with connect_readonly() as conn:
        fetch_run(conn, "segment_state_runs", args.segment_state_run_id)
        fetch_run(conn, "ml_issue_ledger_runs", args.ledger_run_id)
        universe = fetch_universe(conn, args.segment_state_run_id, args.ledger_run_id)

    reviewed = [decide(row) for row in universe[: args.limit]]
    validate_review_rows(reviewed, args.limit)
    jsonl_path, txt_path, decision_counts, subpolicy_counts = write_reports(
        reviewed,
        segment_state_run_id=args.segment_state_run_id,
        ledger_run_id=args.ledger_run_id,
        universe_count=len(universe),
    )
    ready_count = sum(1 for row in reviewed if row["requires_lifecycle_later"])
    apply_count = sum(1 for row in reviewed if row["requires_apply_later"])
    print(f"segment_state_run_id={args.segment_state_run_id}")
    print(f"ledger_run_id={args.ledger_run_id}")
    print(f"eligible_exact_combo_universe={len(universe)}")
    print(f"total_reviewed={len(reviewed)}")
    print(f"ready_for_future_lifecycle={ready_count}")
    print(f"future_apply_candidates={apply_count}")
    print("decision_counts=" + json.dumps(dict(sorted(decision_counts.items())), ensure_ascii=False, sort_keys=True))
    print("subpolicy_counts=" + json.dumps(dict(sorted(subpolicy_counts.items())), ensure_ascii=False, sort_keys=True))
    print(f"recommendation={recommendation(decision_counts)}")
    print(f"jsonl_report={jsonl_path}")
    print(f"txt_report={txt_path}")


if __name__ == "__main__":
    main()
