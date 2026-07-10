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


PAIR_FAMILIES = ["autofix_unknown_microagent", "semantic_review_router"]

TOKEN_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("Select_CString", re.compile(r"Select_CString", re.IGNORECASE)),
    ("Custom", re.compile(r"\bCustom(?:\(|Loc|Localization)?\b", re.IGNORECASE)),
    ("Concept", re.compile(r"\[[^\]|]+[|][^\]]+\]|\bConcept(?:\(|\b)|\$[^$]*concept[^$]*\$", re.IGNORECASE)),
    ("ScriptValue", re.compile(r"\b(?:ScriptValue|GetScriptValue|script_value)\b", re.IGNORECASE)),
    ("GetTrait", re.compile(r"\b(?:GetTrait|Trait|trait_)[A-Za-z0-9_]*", re.IGNORECASE)),
    ("GetExpression", re.compile(r"\bGet[A-Za-z0-9_]*\b", re.IGNORECASE)),
    ("BracketExpression", re.compile(r"\[[^\]]+\]")),
    ("DollarExpression", re.compile(r"\$[^$]+\$")),
    ("ScopeExpression", re.compile(r"\b(?:ROOT|CHARACTER|TARGET|SCOPE|THIS)\.", re.IGNORECASE)),
]

DYNAMIC_RE = re.compile(
    r"\[[^\]]+\]|\$[^$]+\$|\b(?:Get[A-Za-z0-9_]*|Custom|Select_CString|ScriptValue|Concept|ROOT\.|CHARACTER\.|TARGET\.|SCOPE\.|THIS\.)\b",
    re.IGNORECASE,
)
GENDER_RE = re.compile(
    r"ES_(?:OA|XA|EA|ElLa|DelDela|A|O)\b|Get(?:SheHe|HerHis|WomanMan|WomenMen)",
    re.IGNORECASE,
)
CUSTOM_LOC_RE = re.compile(r"custom_localization|custom_loc|CustomLoc|Custom\(", re.IGNORECASE)
DOMAIN_RE = re.compile(
    r"historical_characters|culture|religion|faith|holy_order|law|succession|title|rank|"
    r"trait_|nickname|dynasty|house|government|factions|ai_personality|great_project|"
    r"coat_of_arms|artifact|buildings|regiment|acclaimed_knight|diarch",
    re.IGNORECASE,
)
EVENT_RE = re.compile(
    r"event|interaction|faction|contract_scheme|major_decisions|bookmark|\.desc$|desc\.|option|toast|"
    r"memory|memories|activity|travel|journey|tourism",
    re.IGNORECASE,
)
EFFECT_LIST_RE = re.compile(
    r"tooltip|_tt$|game_rules|effect|modifier|trigger|required|requirement|list|every_|add_|remove_|set_",
    re.IGNORECASE,
)
RESIDUAL_RE = re.compile(
    r"\b(the|will|must|cannot|should|kingdom|county|duchy|royals|hostile|opinion|"
    r"el|la|los|las|una|uno|verdadero|verdadera|fuerza|probabilidad)\b",
    re.IGNORECASE,
)
NEW_MICROAGENT_RE = re.compile(
    r"combat|weapon|vassal|stance|claim|casus|belli|prefix|epithet|landed|accolade|knight",
    re.IGNORECASE,
)
WORD_RE = re.compile(r"[\w']+", re.UNICODE)


def as_text(value: Any) -> str:
    return "" if value is None else str(value)


def current_text(row: dict[str, Any]) -> str:
    for column in ("spanish_text", "old_text", "english_text"):
        value = as_text(row.get(column))
        if value:
            return value
    return ""


def word_count(value: str) -> int:
    cleaned = re.sub(r"\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_:.{};,|]+|#!", " ", value)
    return len(WORD_RE.findall(cleaned))


def tokens_seen(text: str) -> list[str]:
    found: list[str] = []
    for label, pattern in TOKEN_PATTERNS:
        if pattern.search(text):
            found.append(label)
    return found


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


def read_reviewed_segment_ids(paths: list[str]) -> set[int]:
    reviewed: set[int] = set()
    for value in paths:
        path = db.project_path(value)
        if not path.exists():
            raise SystemExit(f"Reviewed JSONL not found: {path}")
        for row in read_jsonl(path):
            if "segment_id" in row:
                reviewed.add(int(row["segment_id"]))
    return reviewed


def read_dynamic_ids_from_saturation(path_value: str) -> tuple[set[int], int]:
    path = db.project_path(path_value)
    if not path.exists():
        raise SystemExit(f"Saturation JSONL not found: {path}")
    rows = read_jsonl(path)
    summary_dynamic = 0
    dynamic_ids: set[int] = set()
    for row in rows:
        if row.get("type") == "summary":
            summary_dynamic = int(row.get("bucket_counts", {}).get("dynamic") or 0)
        elif row.get("type") == "sample" and row.get("bucket") == "dynamic":
            dynamic_ids.add(int(row["segment_id"]))
    return dynamic_ids, summary_dynamic


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_pair_rows(conn: sqlite3.Connection, segment_state_run_id: int, ledger_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        WITH open_issues AS (
            SELECT
                segment_id,
                COUNT(*) AS open_issue_count,
                SUM(CASE WHEN issue_family = 'autofix_unknown_microagent' THEN 1 ELSE 0 END) AS autofix_count,
                SUM(CASE WHEN issue_family = 'semantic_review_router' THEN 1 ELSE 0 END) AS semantic_count,
                SUM(CASE WHEN issue_family NOT IN ('autofix_unknown_microagent', 'semantic_review_router') THEN 1 ELSE 0 END) AS other_issue_count,
                GROUP_CONCAT(DISTINCT issue_family) AS issue_families,
                GROUP_CONCAT(DISTINCT issue_kind) AS issue_kinds
            FROM ml_issue_ledger_items
            WHERE run_id = ?
              AND status = 'open'
            GROUP BY segment_id
        )
        SELECT
            s.segment_id,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.final_state,
            s.state_group,
            s.needs_reopen,
            s.needs_output_apply,
            s.confirmed_matches_output,
            s.priority_score,
            src.spanish_text,
            src.english_text,
            src.old_text,
            oi.open_issue_count,
            oi.autofix_count,
            oi.semantic_count,
            oi.other_issue_count,
            oi.issue_families,
            oi.issue_kinds
        FROM open_issues oi
        JOIN segment_state_items s
          ON s.segment_id = oi.segment_id
         AND s.run_id = ?
        LEFT JOIN source_segments src
          ON src.id = oi.segment_id
        WHERE s.state_group = 'pending'
          AND oi.autofix_count > 0
          AND oi.semantic_count > 0
        ORDER BY s.priority_score DESC, s.segment_id
        """,
        (ledger_run_id, segment_state_run_id),
    ).fetchall()
    return [dict(row) for row in rows]


def clean_for_ready(row: dict[str, Any], text: str, haystack: str) -> bool:
    return (
        row.get("state_group") == "pending"
        and int(row.get("needs_output_apply") or 0) == 0
        and int(row.get("confirmed_matches_output") or 0) == 1
        and int(row.get("needs_reopen") or 0) == 1
        and row.get("final_state") == "reopen_auto_confirmed_autofix"
        and not RESIDUAL_RE.search(text)
        and not DOMAIN_RE.search(haystack)
        and not EVENT_RE.search(haystack)
        and not GENDER_RE.search(haystack)
        and text.count("[") == text.count("]")
        and text.count("$") % 2 == 0
    )


def decide(row: dict[str, Any]) -> dict[str, Any]:
    text = current_text(row)
    haystack = " ".join(
        [
            as_text(row.get("relative_path")),
            as_text(row.get("source_key")),
            as_text(row.get("issue_kinds")),
            as_text(row.get("issue_families")),
            text,
        ]
    )
    seen = tokens_seen(text)
    wc = word_count(text)
    lifecycle_ready = clean_for_ready(row, text, haystack)
    has_multiline = "\n" in text or "\\n" in text

    if GENDER_RE.search(haystack):
        decision = "needs_gender_or_custom_loc_policy"
        subpolicy = "gender_or_custom_loc_dynamic"
        lifecycle_ready = False
    elif CUSTOM_LOC_RE.search(haystack):
        decision = "dynamic_ready_custom_loc_false_reopen" if lifecycle_ready else "needs_custom_loc_policy"
        subpolicy = "custom_loc_expression"
    elif "Select_CString" in seen:
        decision = "dynamic_ready_select_cstring_false_reopen" if lifecycle_ready else "needs_select_cstring_composer"
        subpolicy = "select_cstring_expression"
    elif "Concept" in seen:
        decision = "dynamic_ready_concept_expression_false_reopen" if lifecycle_ready else "needs_concept_expression_policy"
        subpolicy = "concept_expression"
    elif "ScriptValue" in seen:
        decision = "needs_script_value_policy"
        subpolicy = "script_value_expression"
    elif "GetTrait" in seen:
        decision = "needs_get_trait_policy"
        subpolicy = "get_trait_or_trait_requirement"
    elif EFFECT_LIST_RE.search(haystack) or has_multiline:
        decision = "dynamic_ready_effect_list_false_reopen" if lifecycle_ready else "needs_effect_list_or_multiline_policy"
        subpolicy = "effect_list_or_multiline_dynamic"
    elif DOMAIN_RE.search(haystack):
        decision = "needs_domain_context"
        subpolicy = "domain_sensitive_dynamic"
    elif EVENT_RE.search(haystack) or wc >= 12:
        decision = "needs_event_context_composer"
        subpolicy = "event_or_contextual_dynamic"
    elif RESIDUAL_RE.search(text):
        decision = "needs_residual_repair"
        subpolicy = "residual_visible_dynamic"
    elif NEW_MICROAGENT_RE.search(haystack):
        decision = "needs_new_microagent"
        subpolicy = "new_dynamic_surface"
    else:
        decision = "blocked_uncertain"
        subpolicy = "unclassified_dynamic"

    requires_lifecycle_later = decision.startswith("dynamic_ready_")
    notes = (
        "dynamic expression appears aligned and eligible for a future narrow lifecycle"
        if requires_lifecycle_later
        else f"route to {decision} before lifecycle closure"
    )
    return {
        "dynamic_decision": decision,
        "dynamic_subpolicy": subpolicy,
        "tokens_seen": seen,
        "requires_lifecycle_later": requires_lifecycle_later,
        "requires_apply_later": False,
        "notes": notes,
    }


def output_paths() -> tuple[Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_autofix_semantic_dynamic_dominant_sublane_review"
    return base.with_suffix(".jsonl"), base.with_suffix(".txt")


def write_reports(rows: list[dict[str, Any]], universe_dynamic: int, saturation_dynamic_sample: int) -> tuple[Path, Path, Counter[str], Counter[str]]:
    jsonl_path, txt_path = output_paths()
    decision_counts = Counter(row["dynamic_decision"] for row in rows)
    subpolicy_counts = Counter(row["dynamic_subpolicy"] for row in rows)
    jsonl_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )
    lines = [
        "Autofix + semantic dynamic dominant sublane review",
        f"universe_dynamic_estimated: {universe_dynamic:,}",
        f"saturation_dynamic_sample_ids: {saturation_dynamic_sample:,}",
        f"reviewed: {len(rows):,}",
        "",
        "Decision counts:",
    ]
    if decision_counts:
        for key, count in decision_counts.most_common():
            lines.append(f"- {key}: {count:,}")
    else:
        lines.append("- none: 0")
    lines.extend(["", "Top technical sublanes:"])
    if subpolicy_counts:
        for key, count in subpolicy_counts.most_common():
            lines.append(f"- {key}: {count:,}")
    else:
        lines.append("- none: 0")
    ready_count = sum(count for key, count in decision_counts.items() if key.startswith("dynamic_ready_"))
    recommendation = (
        "prepare_narrow_dynamic_lifecycle"
        if ready_count >= 30
        else f"prepare_specific_policy:{subpolicy_counts.most_common(1)[0][0]}"
        if subpolicy_counts and subpolicy_counts.most_common(1)[0][1] >= 50
        else "compare_with_residual_sublane"
    )
    lines.extend(["", f"dynamic_ready_total: {ready_count:,}", f"Recommendation: {recommendation}"])
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return jsonl_path, txt_path, decision_counts, subpolicy_counts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--segment-state-run-id", type=int, required=True)
    parser.add_argument("--ledger-run-id", type=int, required=True)
    parser.add_argument("--saturation-jsonl", required=True)
    parser.add_argument("--limit", type=int, default=240)
    parser.add_argument("--exclude-reviewed-jsonl", action="append", default=[])
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    reviewed_ids = read_reviewed_segment_ids(args.exclude_reviewed_jsonl)
    saturation_dynamic_ids, saturation_dynamic_total = read_dynamic_ids_from_saturation(args.saturation_jsonl)
    with connect_readonly() as conn:
        pair_rows = fetch_pair_rows(conn, args.segment_state_run_id, args.ledger_run_id)
    candidates: list[dict[str, Any]] = []
    dynamic_universe = 0
    for row in pair_rows:
        segment_id = int(row["segment_id"])
        if segment_id in reviewed_ids:
            continue
        text = current_text(row)
        is_dynamic = segment_id in saturation_dynamic_ids or bool(DYNAMIC_RE.search(text))
        if not is_dynamic:
            continue
        dynamic_universe += 1
        if len(candidates) < args.limit:
            decision = decide(row)
            candidates.append(
                {
                    "segment_id": segment_id,
                    "key": row["source_key"],
                    "relative_path": row["relative_path"],
                    "current_text": text,
                    "open_issue_families": PAIR_FAMILIES,
                    **decision,
                }
            )
    jsonl_path, txt_path, decision_counts, subpolicy_counts = write_reports(
        candidates,
        max(dynamic_universe, saturation_dynamic_total),
        len(saturation_dynamic_ids),
    )
    ready_total = sum(count for key, count in decision_counts.items() if key.startswith("dynamic_ready_"))
    print(f"dynamic_universe={max(dynamic_universe, saturation_dynamic_total)}")
    print(f"reviewed={len(candidates)}")
    print(f"dynamic_ready={ready_total}")
    print(f"jsonl_report={jsonl_path}")
    print(f"txt_report={txt_path}")
    print("decision_counts=" + json.dumps(dict(decision_counts), ensure_ascii=False, sort_keys=True))
    print("top_sublanes=" + json.dumps(dict(subpolicy_counts.most_common(10)), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
