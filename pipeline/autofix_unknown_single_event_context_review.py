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


TARGET_SOURCE_DECISION = "needs_event_context_composer"

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
RESIDUAL_RE = re.compile(
    r"\b(the|will|must|cannot|should|kingdom|county|duchy|royals|"
    r"el|la|los|las|una|uno|verdadero|verdadera|fuerza|probabilidad|mientras)\b",
    re.IGNORECASE,
)
OPTION_DIALOGUE_RE = re.compile(r"\.(?:a|b|c|d|e|f|g|h|i|j)$|option|dialogue|toast|response|answer", re.IGNORECASE)
UI_TOOLTIP_RE = re.compile(r"tooltip|_tt$|_desc$|game_rules|tutorial|interface|gui|effect", re.IGNORECASE)
MEMORY_ACTIVITY_RE = re.compile(r"memory|memories|activity|activities|travel|journey|tourism|roaming|tournament|contest", re.IGNORECASE)
EVENT_RE = re.compile(r"event|\.desc$|desc\.|story_cycle|scheme|interaction|epidemic|contest|board_game", re.IGNORECASE)
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
            needs_reopen,
            needs_output_apply,
            confirmed_matches_output
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


def clean_ready(row: dict[str, Any], state: dict[str, Any] | None, family_shape: dict[str, Any] | None, text: str, haystack: str) -> bool:
    return bool(
        state
        and family_shape
        and state.get("state_group") == "pending"
        and int(state.get("needs_output_apply") or 0) == 0
        and int(state.get("confirmed_matches_output") or 0) == 1
        and int(family_shape.get("open_issue_count") or 0) == 1
        and int(family_shape.get("autofix_count") or 0) == 1
        and int(family_shape.get("other_family_count") or 0) == 0
        and row.get("requires_apply_later") is not True
        and text.count("[") == text.count("]")
        and text.count("$") % 2 == 0
        and not RESIDUAL_RE.search(text)
        and not DOMAIN_RE.search(haystack)
        and not DYNAMIC_RE.search(text)
        and not GENDER_RE.search(haystack)
    )


def decide(row: dict[str, Any], state: dict[str, Any] | None, family_shape: dict[str, Any] | None) -> dict[str, Any]:
    text = as_text(row.get("current_text"))
    haystack = " ".join([as_text(row.get("relative_path")), as_text(row.get("key")), text])
    wc = word_count(text)
    ready = clean_ready(row, state, family_shape, text, haystack)

    if GENDER_RE.search(haystack):
        decision = "needs_gender_or_custom_loc_policy"
        subpolicy = "event_gender_or_custom_loc"
    elif DYNAMIC_RE.search(text):
        decision = "needs_dynamic_expression_agent"
        subpolicy = "event_dynamic_expression"
    elif DOMAIN_RE.search(haystack):
        decision = "needs_domain_context"
        subpolicy = "event_domain_context"
    elif RESIDUAL_RE.search(text):
        decision = "needs_residual_repair"
        subpolicy = "event_visible_residual"
    elif ready and UI_TOOLTIP_RE.search(haystack) and wc <= 18:
        decision = "single_event_context_ready_ui_tooltip_lifecycle"
        subpolicy = "event_ui_tooltip"
    elif ready and OPTION_DIALOGUE_RE.search(haystack) and wc <= 14:
        decision = "single_event_context_ready_option_or_dialogue_lifecycle"
        subpolicy = "event_option_or_dialogue"
    elif ready and MEMORY_ACTIVITY_RE.search(haystack) and wc <= 18:
        decision = "single_event_context_ready_memory_or_activity_lifecycle"
        subpolicy = "memory_or_activity_context"
    elif ready and EVENT_RE.search(haystack) and wc <= 18:
        decision = "single_event_context_ready_short_event_lifecycle"
        subpolicy = "short_event_context"
    elif NEW_MICROAGENT_RE.search(haystack):
        decision = "needs_new_microagent"
        subpolicy = "event_new_surface"
    elif wc > 18:
        decision = "needs_plain_prose_context_composer"
        subpolicy = "event_plain_prose_context"
    elif EVENT_RE.search(haystack) or MEMORY_ACTIVITY_RE.search(haystack) or OPTION_DIALOGUE_RE.search(haystack):
        decision = "needs_event_context_composer"
        subpolicy = "event_context_composer_needed"
    else:
        decision = "blocked_uncertain"
        subpolicy = "unclassified_event_context"

    requires_lifecycle = decision.startswith("single_event_context_ready_")
    return {
        "event_context_decision": decision,
        "event_context_subpolicy": subpolicy,
        "requires_lifecycle_later": requires_lifecycle,
        "requires_apply_later": False,
        "notes": (
            "single-family event context appears aligned and suitable for future narrow lifecycle"
            if requires_lifecycle
            else f"route to {decision} before lifecycle closure"
        ),
    }


def output_paths() -> tuple[Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_autofix_unknown_single_event_context_review"
    return base.with_suffix(".jsonl"), base.with_suffix(".txt")


def write_reports(rows: list[dict[str, Any]]) -> tuple[Path, Path, Counter[str], Counter[str]]:
    jsonl_path, txt_path = output_paths()
    decision_counts = Counter(row["event_context_decision"] for row in rows)
    subpolicy_counts = Counter(row["event_context_subpolicy"] for row in rows)
    ready_total = sum(count for key, count in decision_counts.items() if key.startswith("single_event_context_ready_"))
    if ready_total >= 10:
        recommendation = "prepare_narrow_single_event_context_lifecycle"
    elif decision_counts["needs_event_context_composer"] >= max(ready_total, 1):
        recommendation = "close_this_cut_and_consider_residual_apply_protected"
    else:
        dominant = subpolicy_counts.most_common(1)[0] if subpolicy_counts else ("none", 0)
        recommendation = f"prepare_specific_sublane:{dominant[0]}"

    jsonl_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )
    lines = [
        "Autofix unknown single-family event context review",
        f"reviewed: {len(rows):,}",
        "",
        "Decision counts:",
    ]
    if decision_counts:
        for key, count in decision_counts.most_common():
            lines.append(f"- {key}: {count:,}")
    else:
        lines.append("- none: 0")
    lines.extend(["", "Top event/context subpolicies:"])
    if subpolicy_counts:
        for key, count in subpolicy_counts.most_common():
            lines.append(f"- {key}: {count:,}")
    else:
        lines.append("- none: 0")
    lines.extend(["", f"single_event_context_ready_total: {ready_total:,}", f"Recommendation: {recommendation}"])
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return jsonl_path, txt_path, decision_counts, subpolicy_counts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--single-family-jsonl", required=True)
    parser.add_argument("--segment-state-run-id", type=int, required=True)
    parser.add_argument("--ledger-run-id", type=int, default=76)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    source_rows = [
        row
        for row in read_jsonl(db.project_path(args.single_family_jsonl))
        if row.get("decision") == TARGET_SOURCE_DECISION
    ]
    segment_ids = [int(row["segment_id"]) for row in source_rows]
    with connect_readonly() as conn:
        states = fetch_states(conn, args.segment_state_run_id, segment_ids)
        family_shapes = fetch_family_shapes(conn, args.ledger_run_id, segment_ids)

    reviewed: list[dict[str, Any]] = []
    for row in source_rows:
        segment_id = int(row["segment_id"])
        event_context = decide(row, states.get(segment_id), family_shapes.get(segment_id))
        reviewed.append(
            {
                "segment_id": segment_id,
                "key": row["key"],
                "relative_path": row["relative_path"],
                "current_text": row["current_text"],
                "source_decision": row["decision"],
                **event_context,
            }
        )
    jsonl_path, txt_path, decision_counts, subpolicy_counts = write_reports(reviewed)
    ready_total = sum(count for key, count in decision_counts.items() if key.startswith("single_event_context_ready_"))
    print(f"reviewed={len(reviewed)}")
    print(f"single_event_context_ready={ready_total}")
    print(f"jsonl_report={jsonl_path}")
    print(f"txt_report={txt_path}")
    print("decision_counts=" + json.dumps(dict(decision_counts), ensure_ascii=False, sort_keys=True))
    print("top_subpolicies=" + json.dumps(dict(subpolicy_counts.most_common(10)), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
