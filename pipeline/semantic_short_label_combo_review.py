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


TARGET_FAMILIES = ["semantic_review_router", "short_label_style_microagent"]
DEFAULT_EXCLUDE_JSONLS = [
    "reports/20260619_160218_360957_short_label_style_current_high_impact_sublane_review.jsonl",
    "reports/20260619_163552_427911_short_label_style_current_high_impact_sublane_review_batch2.jsonl",
    "reports/20260619_171202_072876_short_label_style_current_high_impact_sublane_review_batch3.jsonl",
    "reports/20260618_195150_476669_short_label_semantic_pair_current_reviewed_chat.jsonl",
    "reports/20260618_221620_156873_short_label_semantic_pair_current_reviewed_chat.jsonl",
    "reports/20260619_000414_518736_short_label_semantic_pair_current_reviewed_chat.jsonl",
    "reports/20260619_011507_874407_short_label_semantic_pair_current_reviewed_chat.jsonl",
]

DYNAMIC_RE = re.compile(
    r"\[[^\]]+\]|\$[^$]+\$|Select_CString|Custom\(|ScriptValue|Concept|"
    r"\bGet[A-Za-z0-9_]*\b|\b(?:ROOT|CHARACTER|TARGET|SCOPE|THIS)\.",
    re.IGNORECASE,
)
GENDER_RE = re.compile(
    r"ES_(?:OA|XA|EA|ElLa|DelDela|A|O)\b|Get(?:SheHe|HerHis|WomanMan|WomenMen)|custom_loc",
    re.IGNORECASE,
)
DOMAIN_RE = re.compile(
    r"historical_characters|culture|religion|faith|holy_order|law|succession|title|rank|"
    r"trait_|nickname|dynasty|house|government|factions|ai_personality|great_project|"
    r"artifact|buildings|regiment|acclaimed_knight|diarch|realm|war|schemes",
    re.IGNORECASE,
)
EVENT_RE = re.compile(
    r"event|\.desc$|desc\.|option|toast|story_cycle|scheme|interaction|memory|memories|"
    r"activity|activities|travel|journey|tourism|roaming|contest|tournament|board_game",
    re.IGNORECASE,
)
UI_RE = re.compile(r"tooltip|_tt$|_desc$|game_rules|tutorial|interface|gui|button|label|name", re.IGNORECASE)
QUOTE_RE = re.compile(r"[\"“”'«»]")
RESIDUAL_RE = re.compile(
    r"\b(the|will|must|cannot|should|kingdom|county|duchy|royals|"
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


def current_text(row: dict[str, Any]) -> str:
    for column in ("spanish_text", "old_text", "english_text"):
        value = as_text(row.get(column))
        if value:
            return value
    return ""


def word_count(text: str) -> int:
    cleaned = re.sub(r"\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_:.{};,|]+|#!", " ", text)
    return len(WORD_RE.findall(cleaned))


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


def load_excluded_ids(paths: list[str]) -> tuple[set[int], list[str]]:
    excluded: set[int] = set()
    used: list[str] = []
    for value in paths:
        path = db.project_path(value)
        if not path.exists():
            continue
        used.append(value)
        for row in read_jsonl(path):
            if "segment_id" in row:
                excluded.add(int(row["segment_id"]))
    return excluded, used


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_candidates(conn: sqlite3.Connection, segment_state_run_id: int, ledger_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        WITH open_issues AS (
            SELECT
                segment_id,
                COUNT(*) AS open_issue_count,
                SUM(CASE WHEN issue_family = 'semantic_review_router' THEN 1 ELSE 0 END) AS semantic_count,
                SUM(CASE WHEN issue_family = 'short_label_style_microagent' THEN 1 ELSE 0 END) AS short_label_count,
                SUM(CASE WHEN issue_family NOT IN ('semantic_review_router', 'short_label_style_microagent') THEN 1 ELSE 0 END) AS other_family_count,
                SUM(
                    CASE
                        WHEN issue_family NOT IN ('semantic_review_router', 'short_label_style_microagent')
                         AND lower(issue_severity) IN ('high', 'error', 'critical')
                        THEN 1 ELSE 0
                    END
                ) AS high_out_of_pair_count,
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
            oi.semantic_count,
            oi.short_label_count,
            oi.other_family_count,
            oi.high_out_of_pair_count,
            oi.issue_families,
            oi.issue_kinds
        FROM open_issues oi
        JOIN segment_state_items s
          ON s.segment_id = oi.segment_id
         AND s.run_id = ?
        LEFT JOIN source_segments src
          ON src.id = oi.segment_id
        WHERE s.state_group = 'pending'
          AND oi.semantic_count > 0
          AND oi.short_label_count > 0
        ORDER BY
            CASE WHEN oi.open_issue_count = 2 AND oi.other_family_count = 0 THEN 0 ELSE 1 END,
            CASE WHEN oi.high_out_of_pair_count = 0 THEN 0 ELSE 1 END,
            CASE
                WHEN length(COALESCE(src.spanish_text, src.old_text, src.english_text, '')) <= 80 THEN 0
                WHEN length(COALESCE(src.spanish_text, src.old_text, src.english_text, '')) <= 160 THEN 1
                ELSE 2
            END,
            s.priority_score DESC,
            s.segment_id
        """,
        (ledger_run_id, segment_state_run_id),
    ).fetchall()
    return [dict(row) for row in rows]


def ready_guard(row: dict[str, Any], text: str, haystack: str) -> bool:
    return (
        row.get("state_group") == "pending"
        and int(row.get("needs_output_apply") or 0) == 0
        and int(row.get("confirmed_matches_output") or 0) == 1
        and text.count("[") == text.count("]")
        and text.count("$") % 2 == 0
        and not RESIDUAL_RE.search(text)
        and not DOMAIN_RE.search(haystack)
        and not DYNAMIC_RE.search(text)
        and not GENDER_RE.search(haystack)
    )


def decide(row: dict[str, Any]) -> dict[str, Any]:
    text = current_text(row)
    haystack = " ".join([as_text(row.get("relative_path")), as_text(row.get("source_key")), as_text(row.get("issue_kinds")), text])
    wc = word_count(text)
    ready = ready_guard(row, text, haystack)
    false_reopen = (
        ready
        and row.get("final_state") == "reopen_auto_confirmed_autofix"
        and int(row.get("needs_reopen") or 0) == 1
    )

    if false_reopen:
        decision = "semantic_short_label_companion_ready_false_reopen"
        subpolicy = "semantic_short_label_false_reopen"
    elif GENDER_RE.search(haystack):
        decision = "needs_gender_or_custom_loc_policy"
        subpolicy = "gender_or_custom_loc"
    elif DYNAMIC_RE.search(text):
        decision = "needs_dynamic_expression_agent"
        subpolicy = "dynamic_expression"
    elif DOMAIN_RE.search(haystack):
        decision = "needs_domain_context"
        subpolicy = "domain_context"
    elif EVENT_RE.search(haystack):
        decision = "needs_event_context_composer"
        subpolicy = "event_context"
    elif RESIDUAL_RE.search(text):
        decision = "needs_residual_repair"
        subpolicy = "visible_residual"
    elif ready and QUOTE_RE.search(text) and wc <= 8:
        decision = "semantic_short_label_ready_quote_fragment"
        subpolicy = "quote_fragment"
    elif ready and UI_RE.search(haystack) and wc <= 8:
        decision = "semantic_short_label_ready_compact_ui_label"
        subpolicy = "compact_ui_label"
    elif ready and wc <= 3:
        decision = "semantic_short_label_ready_nominal_label"
        subpolicy = "nominal_label"
    elif ready and wc <= 8:
        decision = "semantic_short_label_ready_short_phrase"
        subpolicy = "short_phrase"
    elif ready and wc <= 12:
        decision = "semantic_short_label_ready_plain_noop"
        subpolicy = "plain_noop"
    elif NEW_MICROAGENT_RE.search(haystack):
        decision = "needs_new_microagent"
        subpolicy = "new_microagent_surface"
    elif wc > 12:
        decision = "needs_context_composer"
        subpolicy = "context_composer"
    else:
        decision = "blocked_uncertain"
        subpolicy = "uncertain_semantic_short_label"

    requires_lifecycle = decision.startswith("semantic_short_label_ready_") or decision == "semantic_short_label_companion_ready_false_reopen"
    return {
        "decision": decision,
        "subpolicy": subpolicy,
        "requires_lifecycle_later": requires_lifecycle,
        "requires_apply_later": False,
        "notes": (
            "semantic+short-label segment appears aligned and suitable for future lifecycle/bridge"
            if requires_lifecycle
            else f"route to {decision} before lifecycle closure"
        ),
    }


def output_paths() -> tuple[Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_semantic_short_label_combo_review"
    return base.with_suffix(".jsonl"), base.with_suffix(".txt")


def write_reports(rows: list[dict[str, Any]], universe: int, used_excludes: list[str]) -> tuple[Path, Path, Counter[str], Counter[str]]:
    jsonl_path, txt_path = output_paths()
    decision_counts = Counter(row["decision"] for row in rows)
    subpolicy_counts = Counter(row["subpolicy"] for row in rows)
    lifecycle_total = sum(
        count
        for key, count in decision_counts.items()
        if key.startswith("semantic_short_label_ready_") or key == "semantic_short_label_companion_ready_false_reopen"
    )
    companion_total = decision_counts["semantic_short_label_companion_ready_false_reopen"]
    apply_total = decision_counts["needs_residual_repair"]
    if lifecycle_total >= 70:
        recommendation = "prepare_semantic_short_label_lifecycle_read_only"
    elif companion_total >= 30:
        recommendation = "prepare_semantic_short_label_companion_bridge"
    else:
        recommendation = "compare_with_dynamic_ck3_expression_plus_gender_token_or_next_global_bottleneck"

    jsonl_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )
    lines = [
        "Semantic + short-label combo review",
        f"eligible_universe_after_exclusions: {universe:,}",
        f"reviewed: {len(rows):,}",
        "",
        "Excluded JSONLs used:",
    ]
    if used_excludes:
        lines.extend(f"- {path}" for path in used_excludes)
    else:
        lines.append("- none: 0")
    lines.extend(["", "Decision counts:"])
    if decision_counts:
        for key, count in decision_counts.most_common():
            lines.append(f"- {key}: {count:,}")
    else:
        lines.append("- none: 0")
    lines.extend(["", "Top patterns/subpolicies:"])
    if subpolicy_counts:
        for key, count in subpolicy_counts.most_common():
            lines.append(f"- {key}: {count:,}")
    else:
        lines.append("- none: 0")
    lines.extend(
        [
            "",
            f"lifecycle_candidates_future: {lifecycle_total:,}",
            f"companion_ready_false_reopen: {companion_total:,}",
            f"apply_candidates_future: {apply_total:,}",
            f"Recommendation: {recommendation}",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return jsonl_path, txt_path, decision_counts, subpolicy_counts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--segment-state-run-id", type=int, required=True)
    parser.add_argument("--ledger-run-id", type=int, required=True)
    parser.add_argument("--limit", type=int, default=240)
    parser.add_argument("--exclude-reviewed-jsonl", action="append", default=[])
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    exclude_paths = args.exclude_reviewed_jsonl or DEFAULT_EXCLUDE_JSONLS
    excluded_ids, used_excludes = load_excluded_ids(exclude_paths)
    with connect_readonly() as conn:
        candidates = fetch_candidates(conn, args.segment_state_run_id, args.ledger_run_id)
    remaining = [row for row in candidates if int(row["segment_id"]) not in excluded_ids]
    reviewed: list[dict[str, Any]] = []
    for row in remaining[: args.limit]:
        decision = decide(row)
        reviewed.append(
            {
                "segment_id": int(row["segment_id"]),
                "key": row["source_key"],
                "relative_path": row["relative_path"],
                "current_text": current_text(row),
                "open_issue_families": TARGET_FAMILIES,
                **decision,
            }
        )
    jsonl_path, txt_path, decision_counts, subpolicy_counts = write_reports(reviewed, len(remaining), used_excludes)
    lifecycle_total = sum(
        count
        for key, count in decision_counts.items()
        if key.startswith("semantic_short_label_ready_") or key == "semantic_short_label_companion_ready_false_reopen"
    )
    print(f"eligible_universe={len(remaining)}")
    print(f"reviewed={len(reviewed)}")
    print(f"lifecycle_candidates={lifecycle_total}")
    print(f"apply_candidates={decision_counts['needs_residual_repair']}")
    print("excluded_jsonls_used=" + json.dumps(used_excludes, ensure_ascii=False))
    print(f"jsonl_report={jsonl_path}")
    print(f"txt_report={txt_path}")
    print("decision_counts=" + json.dumps(dict(decision_counts), ensure_ascii=False, sort_keys=True))
    print("top_subpolicies=" + json.dumps(dict(subpolicy_counts.most_common(10)), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
