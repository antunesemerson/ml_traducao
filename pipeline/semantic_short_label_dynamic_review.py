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


TOKEN_RE = re.compile(
    r"\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_:.{};,|]+|#!|@[A-Za-z0-9_]+!"
)
SELECT_CSTRING_RE = re.compile(r"Select_CString", re.IGNORECASE)
CUSTOM_LOC_RE = re.compile(r"Custom\(|custom_loc|GetDesignateTooltip|Window\.Get", re.IGNORECASE)
CONCEPT_RE = re.compile(
    r"\[[A-Za-z0-9_]+\|[^\]]+\]|\$game_concept[^$]*\$|Concept\(|\[(?:decision|county|title|claimed|control|"
    r"dynast|herd|gold|capital|situation|migration|stress|imperial_examinations|designated_heir|"
    r"military_building|dynasty_prestige)[^\]]*\]",
    re.IGNORECASE,
)
SCRIPT_VALUE_RE = re.compile(r"ScriptValue|GetScriptValue|script_value", re.IGNORECASE)
GET_TRAIT_RE = re.compile(r"GetTrait|trait_requirement|trait_", re.IGNORECASE)
GENDER_RE = re.compile(
    r"ES_(?:OA|XA|EA|ElLa|DelDela|A|O)\b|Get(?:SheHe|HerHis|WomanMan|WomenMen)",
    re.IGNORECASE,
)
GETTER_RE = re.compile(r"\[[^\]]*\bGet[A-Za-z0-9_]*(?:\.|[|\]])")
EFFECT_LIST_RE = re.compile(
    r"\\n|\n|effects_l_|_effect|_tt$|tooltip|#low|#high|#P|#T|@(?:gold|herd)_icon!",
    re.IGNORECASE,
)
DOMAIN_RE = re.compile(
    r"artifact|title|county|faith|dynasty|realm|capital|migration|situation|building|military|"
    r"court_position|executioner|heir|dominion|claim|claimed|control|government|merit|"
    r"imperial_examinations|tournament|sword|herd|gold|law|religion|culture",
    re.IGNORECASE,
)
EVENT_RE = re.compile(
    r"event|\.desc|desc\.|option|toast|scheme|ongoing|outcome|memory|memories|travel|"
    r"childhood|court_|diplomacy_|learning_|petition_|natural_disaster|laamps|governor",
    re.IGNORECASE,
)
RESIDUAL_RE = re.compile(
    r"\b(?:aumenta|consiguio|gana|ganaste|ganar|todo|tendras|lograste|acepta|torrente|"
    r"posesion|azar|conseguido|paliza|mensual|cual|sitio|enviar|alguaciles|"
    r"conceder|reclamacion|sera|mas|facil|revela|falsedad|establecido|ensenar|"
    r"provecho|poder|desheredar|manipulas|atacante|encuentra|monta|numero|"
    r"vergonzoso|elegir|enfoque|sufre|deshidratacion|pelea|ubicada|bufon|"
    r"corte|entretenga|bajar|destrozo|juguete|profan[oó]|piedra|runica|"
    r"encamamos|encamasteis|repaso|avergonzaste|asesin[oó]|apodere|pastos|"
    r"perdiste|duelo|pres[eé]ntate|ascender|cambiar)\b",
    re.IGNORECASE,
)
TOKEN_BOUNDARY_RE = re.compile(r"\w\?\w|[\[\]]{2,}|\$\s*\$")


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


def fetch_states(conn: sqlite3.Connection, run_id: int, segment_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not segment_ids:
        return {}
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT
            segment_id,
            final_state,
            state_group,
            needs_output_apply,
            confirmed_matches_output,
            needs_reopen,
            is_closed
        FROM segment_state_items
        WHERE run_id = ?
          AND segment_id IN ({placeholders})
        """,
        (run_id, *segment_ids),
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def fetch_family_shapes(conn: sqlite3.Connection, ledger_run_id: int, segment_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not segment_ids:
        return {}
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT
            segment_id,
            COUNT(*) AS open_issue_count,
            SUM(CASE WHEN issue_family = 'semantic_review_router' THEN 1 ELSE 0 END) AS semantic_count,
            SUM(CASE WHEN issue_family = 'short_label_style_microagent' THEN 1 ELSE 0 END) AS short_label_count,
            SUM(CASE WHEN issue_family NOT IN ('semantic_review_router', 'short_label_style_microagent') THEN 1 ELSE 0 END) AS other_family_count
        FROM ml_issue_ledger_items
        WHERE run_id = ?
          AND status = 'open'
          AND segment_id IN ({placeholders})
        GROUP BY segment_id
        """,
        (ledger_run_id, *segment_ids),
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def collect_dynamic_rows(combo_path: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for row in read_jsonl(db.project_path(combo_path)):
        if row.get("decision") != "needs_dynamic_expression_agent":
            continue
        segment_id = int(row["segment_id"])
        if segment_id in seen:
            continue
        seen.add(segment_id)
        rows.append(row)
    return rows


def has_exact_open_family_shape(family_shape: dict[str, Any] | None) -> bool:
    return bool(
        family_shape
        and int(family_shape.get("open_issue_count") or 0) == 2
        and int(family_shape.get("semantic_count") or 0) == 1
        and int(family_shape.get("short_label_count") or 0) == 1
        and int(family_shape.get("other_family_count") or 0) == 0
    )


def has_ready_state(state: dict[str, Any] | None) -> bool:
    return bool(
        state
        and state.get("state_group") == "pending"
        and int(state.get("needs_output_apply") or 0) == 0
        and int(state.get("confirmed_matches_output") or 0) == 1
        and int(state.get("is_closed") or 0) == 0
    )


def tokens_seen(text: str) -> list[str]:
    tokens = TOKEN_RE.findall(text)
    seen: list[str] = []
    for token in tokens:
        label = token
        if token.startswith("[") and "GetTrait" in token:
            label = "GetTrait"
        elif token.startswith("[") and "Get" in token:
            label = "GetExpression"
        elif token.startswith("[") and "|" in token:
            label = "Concept"
        elif token.startswith("$"):
            label = "DollarLoc"
        elif token.startswith("#") or token == "#!":
            label = "FormatTag"
        elif token.startswith("@"):
            label = "Icon"
        if label not in seen:
            seen.append(label)
    if SELECT_CSTRING_RE.search(text) and "Select_CString" not in seen:
        seen.append("Select_CString")
    if CUSTOM_LOC_RE.search(text) and "CustomLoc" not in seen:
        seen.append("CustomLoc")
    if SCRIPT_VALUE_RE.search(text) and "ScriptValue" not in seen:
        seen.append("ScriptValue")
    return seen


def ready_decision(row: dict[str, Any], state: dict[str, Any] | None, family_shape: dict[str, Any] | None) -> str | None:
    text = as_text(row.get("current_text"))
    haystack = " ".join([as_text(row.get("relative_path")), as_text(row.get("key")), text])
    if not has_ready_state(state) or not has_exact_open_family_shape(family_shape):
        return None
    if int(state.get("needs_reopen") or 0) != 1:
        return None
    if GENDER_RE.search(haystack) or DOMAIN_RE.search(haystack) or RESIDUAL_RE.search(text):
        return None
    if TOKEN_BOUNDARY_RE.search(text) or text.count("[") != text.count("]") or text.count("$") % 2 != 0:
        return None
    if SELECT_CSTRING_RE.search(text):
        return "semantic_short_label_dynamic_ready_select_cstring_false_reopen"
    if CUSTOM_LOC_RE.search(text):
        return "semantic_short_label_dynamic_ready_custom_loc_false_reopen"
    if CONCEPT_RE.search(text):
        return "semantic_short_label_dynamic_ready_concept_expression_false_reopen"
    if EFFECT_LIST_RE.search(text):
        return "semantic_short_label_dynamic_ready_effect_list_false_reopen"
    return None


def dynamic_policy(row: dict[str, Any]) -> tuple[str, str]:
    text = as_text(row.get("current_text"))
    haystack = " ".join([as_text(row.get("relative_path")), as_text(row.get("key")), text])
    key_path = " ".join([as_text(row.get("relative_path")), as_text(row.get("key"))])

    if GENDER_RE.search(haystack):
        return "needs_gender_or_custom_loc_policy", "gender_or_custom_loc"
    if SELECT_CSTRING_RE.search(text):
        return "needs_select_cstring_composer", "select_cstring"
    if CUSTOM_LOC_RE.search(text):
        return "needs_custom_loc_policy", "custom_loc"
    if SCRIPT_VALUE_RE.search(haystack):
        return "needs_script_value_policy", "script_value"
    if GET_TRAIT_RE.search(haystack):
        return "needs_get_trait_policy", "get_trait"
    if EFFECT_LIST_RE.search(text) or EFFECT_LIST_RE.search(key_path):
        return "needs_effect_list_or_multiline_policy", "effect_list_or_multiline"
    if CONCEPT_RE.search(text):
        return "needs_concept_expression_policy", "concept_expression"
    if GETTER_RE.search(text):
        return "needs_new_microagent", "scope_getter_expression"
    if DOMAIN_RE.search(haystack):
        return "needs_domain_context", "domain_context"
    if EVENT_RE.search(haystack):
        return "needs_event_context_composer", "event_context"
    if RESIDUAL_RE.search(text):
        return "needs_residual_repair", "visible_spanish_or_english_residual"
    return "blocked_uncertain", "blocked_uncertain"


def decide(row: dict[str, Any], state: dict[str, Any] | None, family_shape: dict[str, Any] | None) -> dict[str, Any]:
    text = as_text(row.get("current_text"))
    ready = ready_decision(row, state, family_shape)
    if ready:
        return {
            "dynamic_decision": ready,
            "dynamic_subpolicy": ready.removeprefix("semantic_short_label_dynamic_ready_").removesuffix("_false_reopen"),
            "tokens_seen": tokens_seen(text),
            "requires_lifecycle_later": True,
            "requires_apply_later": False,
            "notes": "dynamic expression appears preserved and suitable for future narrow false-reopen lifecycle",
        }

    decision, subpolicy = dynamic_policy(row)
    if not has_ready_state(state):
        notes = "blocked by segment_state guard; kept out of ready lifecycle"
    elif not has_exact_open_family_shape(family_shape):
        notes = "blocked by open issue family shape guard; kept out of ready lifecycle"
    else:
        notes = f"routed to {decision}; no apply or lifecycle emitted by this review"
    return {
        "dynamic_decision": decision,
        "dynamic_subpolicy": subpolicy,
        "tokens_seen": tokens_seen(text),
        "requires_lifecycle_later": False,
        "requires_apply_later": False,
        "notes": notes,
    }


def output_paths() -> tuple[Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_semantic_short_label_dynamic_review"
    return base.with_suffix(".jsonl"), base.with_suffix(".txt")


def write_reports(rows: list[dict[str, Any]]) -> tuple[Path, Path, Counter[str], Counter[str]]:
    jsonl_path, txt_path = output_paths()
    decision_counts = Counter(row["dynamic_decision"] for row in rows)
    subpolicy_counts = Counter(row["dynamic_subpolicy"] for row in rows)
    ready_count = sum(1 for row in rows if row["dynamic_decision"].startswith("semantic_short_label_dynamic_ready_"))

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    if ready_count >= 10:
        recommendation = "prepare_narrow_readonly_lifecycle"
    elif decision_counts and max(
        count for decision, count in decision_counts.items() if decision.startswith("needs_")
    ) >= 15:
        top_need = max(
            ((decision, count) for decision, count in decision_counts.items() if decision.startswith("needs_")),
            key=lambda item: item[1],
        )[0]
        recommendation = f"prepare_specific_policy_microagent:{top_need}"
    else:
        recommendation = "close_combo_and_return_to_global_diagnostic"

    lines = [
        "Semantic short-label dynamic review",
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
            f"ready_for_future_lifecycle: {ready_count}",
            "apply_candidates_future: 0",
            f"Recommendation: {recommendation}",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return jsonl_path, txt_path, decision_counts, subpolicy_counts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--combo-jsonl", required=True)
    parser.add_argument("--segment-state-run-id", type=int, required=True)
    parser.add_argument("--ledger-run-id", type=int, default=76)
    args = parser.parse_args()

    source_rows = collect_dynamic_rows(args.combo_jsonl)
    segment_ids = [int(row["segment_id"]) for row in source_rows]
    conn = connect_readonly()
    states = fetch_states(conn, args.segment_state_run_id, segment_ids)
    family_shapes = fetch_family_shapes(conn, args.ledger_run_id, segment_ids)

    reviewed: list[dict[str, Any]] = []
    for row in source_rows:
        segment_id = int(row["segment_id"])
        reviewed.append(
            {
                "segment_id": segment_id,
                "key": row["key"],
                "relative_path": row["relative_path"],
                "current_text": row["current_text"],
                "source_decision": "needs_dynamic_expression_agent",
                **decide(row, states.get(segment_id), family_shapes.get(segment_id)),
            }
        )

    jsonl_path, txt_path, decision_counts, subpolicy_counts = write_reports(reviewed)
    ready_count = sum(1 for row in reviewed if row["dynamic_decision"].startswith("semantic_short_label_dynamic_ready_"))
    print(f"total_reviewed={len(reviewed)}")
    print(f"ready_for_future_lifecycle={ready_count}")
    print(f"jsonl_report={jsonl_path}")
    print(f"txt_report={txt_path}")
    print(f"decision_counts={json.dumps(dict(decision_counts), ensure_ascii=False, sort_keys=True)}")
    print(f"subpolicy_counts={json.dumps(dict(subpolicy_counts), ensure_ascii=False, sort_keys=True)}")


if __name__ == "__main__":
    main()
