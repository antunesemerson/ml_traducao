from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


TARGET_SOURCE_DECISION = "needs_context_composer"
ALLOWED_DECISIONS = {
    "context_ready_plain_prose",
    "context_ready_ui_tooltip",
    "context_ready_short_event_context",
    "context_ready_memory_or_activity",
    "needs_event_context_composer",
    "needs_plain_prose_context_composer",
    "needs_domain_context",
    "needs_dynamic_expression_agent",
    "needs_gender_or_custom_loc_policy",
    "needs_residual_repair",
    "needs_new_microagent",
    "blocked_uncertain",
}
READY_DECISIONS = {
    "context_ready_plain_prose",
    "context_ready_ui_tooltip",
    "context_ready_short_event_context",
    "context_ready_memory_or_activity",
}

DYNAMIC_RE = re.compile(
    r"\[[^\]]+\]|\$[^$]+\$|\b(?:Get[A-Za-z0-9_]*|Custom|Select_CString|ScriptValue|Concept)\b"
)
GENDER_RE = re.compile(
    r"ES_(?:OA|XA|EA|ElLa|DelDela|A|O)\b|Get(?:SheHe|HerHis|WomanMan|WomenMen)",
    re.IGNORECASE,
)
RESIDUAL_RE = re.compile(
    r"\b(the|will|must|cannot|should|kingdom|county|duchy|royals|"
    r"el|la|los|las|una|uno|verdadero|verdadera|fuerza|probabilidad)\b",
    re.IGNORECASE,
)
EVENT_CONTEXT_RE = re.compile(
    r"event|interaction|faction|contract_scheme|major_decisions|bookmark|\\.desc$|desc\\.",
    re.IGNORECASE,
)
UI_TOOLTIP_RE = re.compile(r"tutorial|game_rules|tooltip|_tt$|_desc$", re.IGNORECASE)
MEMORY_ACTIVITY_RE = re.compile(r"memories|memory|activities/|journey|travel|tourism|roaming|activity", re.IGNORECASE)
DOMAIN_RE = re.compile(
    r"historical_characters|custom_localization|culture|religion|faith|holy_order|law|succession|"
    r"title|rank|trait_|nickname|dynasty|house|government|factions|ai_personality|great_project",
    re.IGNORECASE,
)
PLAIN_CONTEXT_RE = re.compile(
    r"buildings_l_spanish|artifacts/|court_artifacts|diarchies/|dlc_l_spanish|regiment_l_spanish",
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
                raise SystemExit(f"Invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def fetch_state_rows(conn, state_run_id: int, segment_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not segment_ids:
        return {}
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT
            segment_id,
            state_group,
            final_state,
            needs_reopen,
            needs_output_apply,
            confirmed_matches_output
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


def word_count(text: str) -> int:
    cleaned = re.sub(r"\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_:.{};,|]+|#!", " ", text)
    return len(re.findall(r"\w+", cleaned, flags=re.UNICODE))


def classify(row: dict[str, Any], state: dict[str, Any] | None) -> tuple[str, str, bool, str]:
    text = as_text(row.get("current_text"))
    relative_path = as_text(row.get("relative_path"))
    key = as_text(row.get("key"))
    haystack = f"{relative_path} {key} {text}"
    wc = word_count(text)

    if not state_guards_clean(state):
        return (
            "blocked_uncertain",
            "state_guard_not_clean",
            False,
            "segment is not pending/aligned in the requested segment-state run",
        )
    if bool(row.get("requires_apply_later")):
        return (
            "needs_residual_repair",
            "source_requires_apply_later",
            False,
            "source review marked future apply requirement",
        )
    if text.count("[") != text.count("]") or text.count("$") % 2:
        return (
            "needs_residual_repair",
            "token_boundary_or_markup",
            False,
            "token or markup boundary is not safely balanced",
        )
    if GENDER_RE.search(text):
        return (
            "needs_gender_or_custom_loc_policy",
            "gender_or_custom_loc_context",
            False,
            "gender/custom-loc expression needs dedicated policy",
        )
    if DYNAMIC_RE.search(text):
        return (
            "needs_dynamic_expression_agent",
            "dynamic_or_tokenized_context",
            False,
            "dynamic CK3 token/expression needs dynamic agent before context closure",
        )
    if RESIDUAL_RE.search(text):
        return (
            "needs_residual_repair",
            "visible_language_or_fluency_residual",
            False,
            "visible residual language marker blocks context-ready closure",
        )
    if DOMAIN_RE.search(haystack):
        return (
            "needs_domain_context",
            "domain_or_named_entity_context",
            False,
            "domain-sensitive or named CK3 entity needs policy",
        )
    if MEMORY_ACTIVITY_RE.search(haystack):
        return (
            "context_ready_memory_or_activity",
            "memory_activity_or_travel_context",
            True,
            "memory/activity/travel surface has enough context and clean guards",
        )
    if EVENT_CONTEXT_RE.search(haystack):
        if wc <= 35:
            return (
                "context_ready_short_event_context",
                "short_event_or_interaction_context",
                True,
                "short event/interaction context is governable without output writes",
            )
        return (
            "needs_event_context_composer",
            "event_subject_object_perspective",
            False,
            "event/interactions context is too long or perspective-sensitive",
        )
    if UI_TOOLTIP_RE.search(haystack) and wc <= 35:
        return (
            "context_ready_ui_tooltip",
            "ui_tooltip_or_tutorial_context",
            True,
            "UI/tutorial surface is compact and governable without output writes",
        )
    if PLAIN_CONTEXT_RE.search(haystack):
        return (
            "context_ready_plain_prose",
            "descriptive_plain_prose_context",
            True,
            "descriptive prose surface has enough context and clean guards",
        )
    if wc <= 35:
        return (
            "context_ready_plain_prose",
            "short_plain_prose_context",
            True,
            "short plain prose has enough context and clean guards",
        )
    return (
        "needs_plain_prose_context_composer",
        "medium_long_plain_prose_context",
        False,
        "medium/long prose needs a stronger context composer before lifecycle",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch4-jsonl", required=True)
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    args = parser.parse_args()

    batch4_path = Path(args.batch4_jsonl)
    source_rows = [row for row in read_jsonl(batch4_path) if row.get("decision") == TARGET_SOURCE_DECISION]
    segment_ids = [int(row["segment_id"]) for row in source_rows]

    conn = db.connect()
    state_rows = fetch_state_rows(conn, args.segment_state_run_id, segment_ids)
    conn.close()

    reviewed: list[dict[str, Any]] = []
    for row in source_rows:
        segment_id = int(row["segment_id"])
        decision, subpolicy, lifecycle_later, notes = classify(row, state_rows.get(segment_id))
        if decision not in ALLOWED_DECISIONS:
            raise SystemExit(f"Internal classifier emitted invalid decision: {decision}")
        reviewed.append(
            {
                "segment_id": segment_id,
                "key": row.get("key", ""),
                "relative_path": row.get("relative_path", ""),
                "current_text": row.get("current_text", ""),
                "source_decision": row.get("decision", ""),
                "context_decision": decision,
                "context_subpolicy": subpolicy,
                "requires_lifecycle_later": lifecycle_later,
                "requires_apply_later": False,
                "corrected_text": "",
                "notes": notes,
            }
        )

    settings = db.load_settings()
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    jsonl_path = reports_dir / f"{stamp}_autofix_semantic_context_composer_batch4_review.jsonl"
    txt_path = reports_dir / f"{stamp}_autofix_semantic_context_composer_batch4_review.txt"
    jsonl_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in reviewed) + "\n",
        encoding="utf-8",
    )

    decisions = Counter(row["context_decision"] for row in reviewed)
    subpolicies = Counter(row["context_subpolicy"] for row in reviewed)
    lifecycle_candidates = sum(count for decision, count in decisions.items() if decision in READY_DECISIONS)

    lines = [
        "Autofix semantic context composer batch4 review",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"batch4_jsonl: {batch4_path}",
        f"segment_state_run_id: {args.segment_state_run_id}",
        f"reviewed: {len(reviewed)}",
        f"lifecycle_candidates_future: {lifecycle_candidates}",
        "apply_candidates_future: 0",
        "",
        "Counts by context_decision:",
    ]
    for decision, count in decisions.most_common():
        lines.append(f"- {decision}: {count}")
    lines.extend(["", "Top context subpolicies:"])
    for subpolicy, count in subpolicies.most_common():
        lines.append(f"- {subpolicy}: {count}")
    lines.extend(["", "Main usable patterns:"])
    lines.append("- descriptive building/artifact/diarchy/DLC/regiment prose with clean confirmation/output guards")
    lines.append("- compact memory/activity/travel or short event contexts without dynamic tokens")
    lines.extend(["", "Main blockers:"])
    lines.append("- dynamic CK3 tokens or concept links")
    lines.append("- domain-sensitive named entities, historical names, government/law/faction contexts")
    lines.append("- longer event/interactions text requiring subject/object/perspective context")
    lines.extend(["", "Recommendation:"])
    if lifecycle_candidates >= 70:
        lines.append("- Prepare a lifecycle/composition context bridge for context_ready_* decisions.")
    else:
        needs_counts = {decision: count for decision, count in decisions.items() if decision.startswith("needs_")}
        if needs_counts and max(needs_counts.values()) >= 50:
            top_decision, top_count = Counter(needs_counts).most_common(1)[0]
            lines.append(f"- Prepare a specific microagent/composer for {top_decision} ({top_count}).")
        else:
            lines.append("- Context split is mixed; return to global diagnostic or split next dominant blocker.")
    lines.append("")
    lines.append(
        "Safety confirmation: read-only review only; no production, no apply, no lifecycle, no segment-state, no confirmations, no reindex, no training/model promotion, no source/output edits."
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"reviewed={len(reviewed)}")
    print(f"lifecycle_candidates_future={lifecycle_candidates}")
    print("context_decision_counts=" + json.dumps(decisions, ensure_ascii=False, sort_keys=True))
    print(f"jsonl={jsonl_path}")
    print(f"txt={txt_path}")


if __name__ == "__main__":
    main()
