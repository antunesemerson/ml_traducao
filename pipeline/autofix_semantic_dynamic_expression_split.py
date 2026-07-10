from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


TARGET_DECISION = "needs_dynamic_expression_agent"
ALLOWED_DECISIONS = {
    "dynamic_ready_select_cstring_false_reopen",
    "dynamic_ready_custom_loc_false_reopen",
    "dynamic_ready_concept_expression_false_reopen",
    "needs_select_cstring_composer",
    "needs_custom_loc_policy",
    "needs_concept_expression_policy",
    "needs_script_value_policy",
    "needs_get_trait_policy",
    "needs_gender_or_custom_loc_policy",
    "needs_event_context_composer",
    "needs_domain_context",
    "needs_residual_repair",
    "blocked_uncertain",
}

TOKEN_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("Select_CString", re.compile(r"\bSelect_CString\s*\(")),
    ("Custom", re.compile(r"\bCustom\s*\(")),
    ("Concept", re.compile(r"\bConcept\s*\(")),
    ("ScriptValue", re.compile(r"\bScriptValue\b")),
    ("GetTrait", re.compile(r"\bGetTrait\b")),
    ("gender_getter", re.compile(r"\bGet(?:WomenMen|WomanMan|HerHis|SheHe|HerselfHimself|HerHim)\b")),
    ("gender_custom_loc", re.compile(r"ES_(?:OA|XA|EA|ElLa|DelDela|A|O)\b")),
    ("dollar_concept", re.compile(r"\$game_concept[^$]+\$", re.IGNORECASE)),
    ("bracket_concept", re.compile(r"\[[A-Za-z0-9_.'()|]+\|[A-Za-z]*E\]", re.IGNORECASE)),
    ("bracket_token", re.compile(r"\[[^\]]+\]")),
    ("getter", re.compile(r"\[[^\]]*\bGet[A-Za-z_]*[^\]]*\]")),
    ("var_helper", re.compile(r"\bVar\s*\(")),
]

RESIDUAL_RE = re.compile(
    r"\b("
    r"the|will|must|cannot|should|your|their|kingdom|county|duchy|"
    r"el|la|los|las|una|uno|verdadero|verdadera|fuerza|hombre|mujer|"
    r"ele/ela"
    r")\b",
    re.IGNORECASE,
)
GENDER_RE = re.compile(
    r"ES_(?:OA|XA|EA|ElLa|DelDela|A|O)\b|"
    r"\bGet(?:WomenMen|WomanMan|HerHis|SheHe|HerselfHimself|HerHim)\b|"
    r"ele/ela",
    re.IGNORECASE,
)
EVENT_CONTEXT_RE = re.compile(
    r"event_localization|activities/|journey_activity|roaming_activity|feast_",
    re.IGNORECASE,
)
DOMAIN_CONTEXT_RE = re.compile(
    r"religion|faith|holy_order|iberia|north_africa|war|wars_|cb_|claim|claims|"
    r"title|titles|tributar|confederation|de_jure|vassal|liege|law|culture|"
    r"nick|trait|scheme|seduce|courting",
    re.IGNORECASE,
)


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
                raise SystemExit(f"Invalid JSONL at line {line_number}: {exc}") from exc
    return rows


def extract_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for label, pattern in TOKEN_PATTERNS:
        if pattern.search(text):
            tokens.append(label)
    return tokens


def fetch_state_rows(conn, state_run_id: int, segment_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not segment_ids:
        return {}
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT
            segment_id,
            relative_path,
            source_key,
            state_group,
            needs_output_apply,
            confirmed_matches_output,
            needs_reopen,
            final_state,
            is_closed
        FROM segment_state_items
        WHERE run_id = ?
          AND segment_id IN ({placeholders})
        """,
        (state_run_id, *segment_ids),
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def state_guards_clean(state: dict[str, Any] | None) -> bool:
    if not state:
        return False
    return (
        state.get("state_group") == "pending"
        and int(state.get("needs_output_apply") or 0) == 0
        and int(state.get("confirmed_matches_output") or 0) == 1
    )


def classify(row: dict[str, Any], state: dict[str, Any] | None) -> tuple[str, str, bool, list[str], str]:
    text = as_text(row.get("current_text"))
    relative_path = as_text(row.get("relative_path"))
    key = as_text(row.get("key"))
    haystack = f"{relative_path} {key} {text}"
    tokens = extract_tokens(text)

    if not state_guards_clean(state):
        return (
            "blocked_uncertain",
            "state_guard_not_clean",
            False,
            tokens,
            "segment is not pending/aligned in the requested segment-state run",
        )
    if text.count("[") != text.count("]") or text.count("$") % 2:
        return (
            "needs_residual_repair",
            "dynamic_token_boundary",
            False,
            tokens,
            "dynamic markup boundary is not safely balanced",
        )
    if GENDER_RE.search(text):
        return (
            "needs_gender_or_custom_loc_policy",
            "gender_or_custom_loc_dynamic",
            False,
            tokens,
            "gender-sensitive dynamic expression needs a dedicated policy",
        )
    if RESIDUAL_RE.search(text) and "ele/ela" in text.lower():
        return (
            "needs_gender_or_custom_loc_policy",
            "literal_gender_residual",
            False,
            tokens,
            "literal gender variant remains in the text",
        )
    if RESIDUAL_RE.search(text) and not re.search(r"\b(?:your|their)\b", text, re.IGNORECASE):
        return (
            "needs_residual_repair",
            "visible_language_residual",
            False,
            tokens,
            "visible residual language marker blocks a dynamic-ready close",
        )
    if "Select_CString" in tokens:
        return (
            "needs_select_cstring_composer",
            "select_cstring_branch_semantics",
            False,
            tokens,
            "Select_CString branch semantics need a narrow composer/policy",
        )
    if "ScriptValue" in tokens:
        return (
            "needs_script_value_policy",
            "script_value_expression",
            False,
            tokens,
            "ScriptValue expression needs dedicated policy before closure",
        )
    if "GetTrait" in tokens:
        return (
            "needs_get_trait_policy",
            "get_trait_expression",
            False,
            tokens,
            "GetTrait expression needs dedicated policy before closure",
        )
    if "Custom" in tokens or "var_helper" in tokens:
        return (
            "needs_custom_loc_policy",
            "custom_loc_or_var_helper",
            False,
            tokens,
            "custom localization/helper expression needs policy",
        )
    if "Concept" in tokens or "dollar_concept" in tokens or "bracket_concept" in tokens:
        return (
            "needs_concept_expression_policy",
            "concept_expression_policy",
            False,
            tokens,
            "concept expression is preserved but needs concept policy",
        )
    if EVENT_CONTEXT_RE.search(haystack):
        return (
            "needs_event_context_composer",
            "event_subject_object_perspective",
            False,
            tokens,
            "event/activity perspective needs context before closure",
        )
    if DOMAIN_CONTEXT_RE.search(haystack):
        return (
            "needs_domain_context",
            "ck3_domain_entity_context",
            False,
            tokens,
            "domain-sensitive CK3 entity or rule text needs context",
        )
    if "getter" in tokens:
        return (
            "needs_custom_loc_policy",
            "generic_getter_dynamic_policy",
            False,
            tokens,
            "generic getter token is preserved but still needs dynamic-token policy",
        )
    return (
        "blocked_uncertain",
        "unclassified_dynamic_expression",
        False,
        tokens,
        "dynamic expression did not match a narrow safe sublane",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--review-jsonl", required=True)
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    parser.add_argument("--limit", required=True, type=int)
    args = parser.parse_args()

    review_path = Path(args.review_jsonl)
    rows = read_jsonl(review_path)
    dynamic_rows = [row for row in rows if row.get("decision") == TARGET_DECISION]
    selected_rows = dynamic_rows[: args.limit]
    segment_ids = [int(row["segment_id"]) for row in selected_rows]

    conn = db.connect()
    state_rows = fetch_state_rows(conn, args.segment_state_run_id, segment_ids)
    conn.close()

    reviewed: list[dict[str, Any]] = []
    for row in selected_rows:
        segment_id = int(row["segment_id"])
        decision, subpolicy, lifecycle_later, tokens, notes = classify(row, state_rows.get(segment_id))
        if decision not in ALLOWED_DECISIONS:
            raise SystemExit(f"Internal classifier emitted invalid decision: {decision}")
        reviewed.append(
            {
                "segment_id": segment_id,
                "key": row.get("key", ""),
                "relative_path": row.get("relative_path", ""),
                "current_text": row.get("current_text", ""),
                "dynamic_decision": decision,
                "dynamic_subpolicy": subpolicy,
                "requires_lifecycle_later": lifecycle_later,
                "requires_apply_later": False,
                "corrected_text": "",
                "tokens_seen": tokens,
                "notes": notes,
            }
        )

    settings = db.load_settings()
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    jsonl_path = reports_dir / f"{stamp}_autofix_semantic_dynamic_expression_split.jsonl"
    txt_path = reports_dir / f"{stamp}_autofix_semantic_dynamic_expression_split.txt"

    jsonl_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in reviewed) + "\n",
        encoding="utf-8",
    )

    decisions = Counter(row["dynamic_decision"] for row in reviewed)
    subpolicies = Counter(row["dynamic_subpolicy"] for row in reviewed)
    ready_count = sum(
        count for decision, count in decisions.items() if decision.startswith("dynamic_ready_")
    )
    lines = [
        "Autofix semantic dynamic expression split",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"review_jsonl: {review_path}",
        f"segment_state_run_id: {args.segment_state_run_id}",
        f"source_dynamic_rows: {len(dynamic_rows)}",
        f"reviewed: {len(reviewed)}",
        f"limit: {args.limit}",
        f"dynamic_ready_count: {ready_count}",
        "apply_candidates_future: 0",
        "",
        "Counts by dynamic_decision:",
    ]
    for decision, count in decisions.most_common():
        lines.append(f"- {decision}: {count}")
    lines.extend(["", "Top technical sublanes:"])
    for subpolicy, count in subpolicies.most_common():
        lines.append(f"- {subpolicy}: {count}")
    lines.extend(["", "Recommendation:"])
    if ready_count >= 10:
        lines.append("- Prepare a narrow read-only lifecycle for dynamic_ready_* false reopens.")
    else:
        if subpolicies:
            top_sublane, top_count = subpolicies.most_common(1)[0]
            lines.append(f"- Dynamic-ready yield is below 10; prioritize a microagent for {top_sublane} ({top_count}).")
        else:
            lines.append("- No rows were classified; inspect source review before any lifecycle.")
    lines.extend(
        [
            "",
            "Safety:",
            "- read-only split only",
            "- no apply",
            "- no lifecycle",
            "- no segment-state run",
            "- no confirmations",
            "- no source/output writes",
            "",
            f"JSONL: {jsonl_path}",
            f"TXT: {txt_path}",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"jsonl={jsonl_path}")
    print(f"txt={txt_path}")
    print(f"reviewed={len(reviewed)}")
    print(f"dynamic_ready_count={ready_count}")
    print("dynamic_decisions=" + json.dumps(decisions, ensure_ascii=False, sort_keys=True))
    print("top_sublanes=" + json.dumps(subpolicies.most_common(), ensure_ascii=False))


if __name__ == "__main__":
    main()
