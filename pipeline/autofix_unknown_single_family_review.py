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
    r"artifact|buildings|regiment|acclaimed_knight|diarch|coat_of_arms",
    re.IGNORECASE,
)
EVENT_RE = re.compile(
    r"event|\.desc$|desc\.|option|toast|interaction|scheme|memory|memories|bookmark|story_cycle|"
    r"activity|travel|journey|tourism",
    re.IGNORECASE,
)
UI_TOOLTIP_RE = re.compile(r"tooltip|_tt$|_desc$|game_rules|tutorial|interface|gui", re.IGNORECASE)
MODIFIER_RE = re.compile(r"modifier|opinion|effect|bonus|penalty|description|_desc$", re.IGNORECASE)
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
                SUM(CASE WHEN issue_family = 'autofix_unknown_microagent' THEN 1 ELSE 0 END) AS autofix_count,
                SUM(CASE WHEN issue_family != 'autofix_unknown_microagent' THEN 1 ELSE 0 END) AS other_family_count,
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
            oi.other_family_count,
            oi.issue_families,
            oi.issue_kinds
        FROM open_issues oi
        JOIN segment_state_items s
          ON s.segment_id = oi.segment_id
         AND s.run_id = ?
        LEFT JOIN source_segments src
          ON src.id = oi.segment_id
        WHERE s.state_group = 'pending'
          AND oi.open_issue_count = 1
          AND oi.autofix_count = 1
          AND oi.other_family_count = 0
        ORDER BY
            CASE WHEN s.needs_output_apply = 0 AND s.confirmed_matches_output = 1 THEN 0 ELSE 1 END,
            CASE
                WHEN length(COALESCE(src.spanish_text, src.old_text, src.english_text, '')) <= 120 THEN 0
                WHEN length(COALESCE(src.spanish_text, src.old_text, src.english_text, '')) <= 260 THEN 1
                ELSE 2
            END,
            s.priority_score DESC,
            s.segment_id
        """,
        (ledger_run_id, segment_state_run_id),
    ).fetchall()
    return [dict(row) for row in rows]


def clean_ready(row: dict[str, Any], text: str, haystack: str) -> bool:
    return (
        row.get("state_group") == "pending"
        and int(row.get("open_issue_count") or 0) == 1
        and int(row.get("autofix_count") or 0) == 1
        and int(row.get("other_family_count") or 0) == 0
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
    ready = clean_ready(row, text, haystack)
    false_reopen = (
        ready
        and row.get("final_state") == "reopen_auto_confirmed_autofix"
        and int(row.get("needs_reopen") or 0) == 1
    )

    if false_reopen:
        decision = "single_autofix_ready_false_reopen"
        subpolicy = "single_family_false_reopen_aligned"
    elif GENDER_RE.search(haystack):
        decision = "needs_gender_or_custom_loc_policy"
        subpolicy = "gender_or_custom_loc"
    elif DYNAMIC_RE.search(text):
        decision = "needs_dynamic_expression_agent"
        subpolicy = "dynamic_expression"
    elif DOMAIN_RE.search(haystack):
        decision = "needs_domain_context"
        subpolicy = "domain_sensitive"
    elif EVENT_RE.search(haystack):
        decision = "needs_event_context_composer"
        subpolicy = "event_or_activity_context"
    elif RESIDUAL_RE.search(text):
        decision = "needs_residual_repair"
        subpolicy = "visible_residual"
    elif ready and UI_TOOLTIP_RE.search(haystack):
        decision = "single_autofix_ready_lifecycle"
        subpolicy = "ui_tooltip"
    elif ready and wc <= 4:
        decision = "single_autofix_ready_lifecycle"
        subpolicy = "short_phrase"
    elif ready and MODIFIER_RE.search(haystack) and wc <= 16:
        decision = "single_autofix_ready_lifecycle"
        subpolicy = "modifier_or_description"
    elif ready and wc <= 12:
        decision = "single_autofix_ready_lifecycle"
        subpolicy = "plain_sentence"
    elif NEW_MICROAGENT_RE.search(haystack):
        decision = "needs_new_microagent"
        subpolicy = "new_surface"
    elif wc > 12:
        decision = "needs_plain_prose_context_composer"
        subpolicy = "plain_prose_context"
    else:
        decision = "blocked_uncertain"
        subpolicy = "uncertain_single_autofix"

    requires_lifecycle = decision.startswith("single_autofix_ready_")
    return {
        "single_autofix_decision": decision,
        "single_autofix_subpolicy": subpolicy,
        "requires_lifecycle_later": requires_lifecycle,
        "requires_apply_later": False,
        "corrected_text": "",
        "notes": (
            "single-family autofix segment appears aligned and suitable for future narrow lifecycle"
            if requires_lifecycle
            else f"route to {decision} before lifecycle closure"
        ),
    }


def output_paths(output_suffix: str | None) -> tuple[Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    suffix = f"_{output_suffix}" if output_suffix else ""
    base = reports_dir / f"{stamp}_autofix_unknown_single_family_review{suffix}"
    return base.with_suffix(".jsonl"), base.with_suffix(".txt")


def write_reports(
    rows: list[dict[str, Any]], universe_before_exclusion: int, universe_after_exclusion: int, output_suffix: str | None
) -> tuple[Path, Path, Counter[str], Counter[str]]:
    jsonl_path, txt_path = output_paths(output_suffix)
    decision_counts = Counter(row["single_autofix_decision"] for row in rows)
    subpolicy_counts = Counter(row["single_autofix_subpolicy"] for row in rows)
    lifecycle_total = sum(count for key, count in decision_counts.items() if key.startswith("single_autofix_ready_"))
    apply_total = sum(1 for row in rows if row["requires_apply_later"])
    dominant = subpolicy_counts.most_common(1)[0] if subpolicy_counts else ("none", 0)
    if lifecycle_total >= 70:
        recommendation = "prepare_single_family_lifecycle_read_only"
    elif decision_counts["needs_dynamic_expression_agent"] >= 50:
        recommendation = "prepare_dynamic_expression_split"
    elif decision_counts["needs_event_context_composer"] >= 30:
        recommendation = "prepare_event_context_split"
    elif decision_counts["needs_residual_repair"] >= 5:
        recommendation = "prepare_residual_split_before_apply"
    elif dominant[1] >= 50:
        recommendation = f"prepare_specific_sublane:{dominant[0]}"
    else:
        recommendation = "return_to_global_diagnostic"

    jsonl_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )
    lines = [
        "Autofix unknown single-family review",
        f"eligible_universe_before_exclusion: {universe_before_exclusion:,}",
        f"eligible_universe_after_exclusion: {universe_after_exclusion:,}",
        f"reviewed: {len(rows):,}",
        "",
        "Decision counts:",
    ]
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
    parser.add_argument("--exclude-reviewed-jsonl")
    parser.add_argument("--output-suffix")
    return parser.parse_args()


def read_excluded_ids(path_value: str | None) -> set[int]:
    if not path_value:
        return set()
    path = db.project_path(path_value)
    excluded: set[int] = set()
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"Invalid JSONL at {path}:{line_number}: {exc}") from exc
            excluded.add(int(row["segment_id"]))
    return excluded


def main() -> None:
    args = parse_args()
    with connect_readonly() as conn:
        candidates = fetch_candidates(conn, args.segment_state_run_id, args.ledger_run_id)
    excluded_ids = read_excluded_ids(args.exclude_reviewed_jsonl)
    candidates_after_exclusion = [row for row in candidates if int(row["segment_id"]) not in excluded_ids]
    reviewed: list[dict[str, Any]] = []
    for row in candidates_after_exclusion[: args.limit]:
        text = current_text(row)
        decision = decide(row)
        reviewed.append(
            {
                "segment_id": int(row["segment_id"]),
                "key": row["source_key"],
                "relative_path": row["relative_path"],
                "current_text": text,
                "open_issue_families": [TARGET_FAMILY],
                **decision,
            }
        )
    jsonl_path, txt_path, decision_counts, subpolicy_counts = write_reports(
        reviewed, len(candidates), len(candidates_after_exclusion), args.output_suffix
    )
    lifecycle_total = sum(count for key, count in decision_counts.items() if key.startswith("single_autofix_ready_"))
    print(f"eligible_universe_before_exclusion={len(candidates)}")
    print(f"eligible_universe_after_exclusion={len(candidates_after_exclusion)}")
    print(f"excluded_reviewed_ids={len(excluded_ids)}")
    print(f"overlap_with_excluded={sum(1 for row in reviewed if int(row['segment_id']) in excluded_ids)}")
    print(f"reviewed={len(reviewed)}")
    print(f"lifecycle_candidates={lifecycle_total}")
    print(f"apply_candidates={sum(1 for row in reviewed if row['requires_apply_later'])}")
    print(f"jsonl_report={jsonl_path}")
    print(f"txt_report={txt_path}")
    print("decision_counts=" + json.dumps(dict(decision_counts), ensure_ascii=False, sort_keys=True))
    print("top_subpolicies=" + json.dumps(dict(subpolicy_counts.most_common(10)), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
