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


ALLOWED_DECISIONS = {
    "es_oa_event_ready_false_reopen",
    "es_oa_event_ready_lifecycle",
    "needs_es_oa_event_target_gender_policy",
    "needs_es_oa_event_actor_recipient_policy",
    "needs_es_oa_event_local_player_policy",
    "needs_es_oa_event_subject_object_composer",
    "needs_es_oa_event_longform_composer",
    "needs_es_oa_event_option_tooltip_policy",
    "needs_es_oa_event_domain_context",
    "needs_es_oa_event_title_trait_policy",
    "needs_es_oa_event_residual_repair",
    "needs_new_microagent",
    "blocked_uncertain",
}

TOKEN_RE = re.compile(
    r"Select_CString|Custom\(|ES_(?:OA|XA|EA|ElLa|DelDela|AlAla)|\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_:.{};,|]+|#!",
    re.IGNORECASE,
)
ES_OA_RE = re.compile(r"Custom\(\s*['\"]ES_OA['\"]\s*\)|ES_OA", re.IGNORECASE)
LOCAL_PLAYER_RE = re.compile(r"GetPlayer|ROOT\.Char|local player|você|voc[eę]", re.IGNORECASE)
ACTOR_RECIPIENT_RE = re.compile(
    r"\[(?:recipient|actor|host|woo_actor|local_character|merchant_magnate|saved_devaraja|rowdy_boy|victim|stop_host_heir|[^.\]]+_scope|[^.\]]+_actor|[^.\]]+_recipient)[.\]]",
    re.IGNORECASE,
)
TARGET_SCOPE_RE = re.compile(r"\[(?:target|scope|ROOT|CHARACTER|TARGET_CHARACTER)[.\]]", re.IGNORECASE)
SUBJECT_OBJECT_RE = re.compile(r"GetSheHe|GetHerHim|GetShortUIName|GetFirstName|GetName|GetUIName", re.IGNORECASE)
OPTION_TOOLTIP_RE = re.compile(r"(?:\.tt$|\.trigger$|\.fallback$|\.average$|\.ptv_|_tooltip|tooltip|_option|option|\.a$|\.b$|\.c$|\.d$)", re.IGNORECASE)
TITLE_TRAIT_RE = re.compile(
    r"trait|accolade|knight|title|court_position|governor|devaraja|heir|herdeir|exam|examination|mandala",
    re.IGNORECASE,
)
DOMAIN_RE = re.compile(
    r"tournament|contest|contract|assimilation|health|hunt|travel|nomad|imperial|governor|dinner|classifica[cç][aã]o|evento|exam|devaraja|mandala",
    re.IGNORECASE,
)
RESIDUAL_RE = re.compile(
    r"\b(?:vocę|nă|năo|săo|estăo|entăo|serăo|mism|No#!|devaraja prometid|opção está disponível|vosso|vos|deixar est)\b|desconfortá\[|perdid\[|acordad\[|convencid\[",
    re.IGNORECASE,
)
DIALOGUE_LONGFORM_RE = re.compile(r'"|“|”|\.desc|desc\.|intro|quiet|root_victim|travel_option|yearly|interaction",?', re.IGNORECASE)


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
        if row.get("es_oa_decision") != "needs_es_oa_event_context_composer":
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


def tokens_seen(text: str) -> list[str]:
    labels: list[str] = []
    for label, pattern in [
        ("ES_OA", ES_OA_RE),
        ("LocalPlayer", LOCAL_PLAYER_RE),
        ("ActorRecipient", ACTOR_RECIPIENT_RE),
        ("TargetScope", TARGET_SCOPE_RE),
        ("SubjectObject", SUBJECT_OBJECT_RE),
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


def can_false_reopen(state: dict[str, Any] | None) -> bool:
    if not state_is_pending_confirmed(state):
        return False
    return (
        int(state.get("needs_reopen") or 0) == 1
        and state.get("final_state") == "reopen_auto_confirmed_autofix"
    )


def classify(row: dict[str, Any], state: dict[str, Any] | None) -> tuple[str, str, str]:
    text = row["current_text"]
    key = row["key"]
    haystack = " ".join([row["relative_path"], key, text])

    if row.get("source_dynamic_gender_decision") != "needs_es_oa_policy":
        return "blocked_uncertain", "unexpected_source_branch", "source branch does not trace back to needs_es_oa_policy"
    if not state_is_pending_confirmed(state):
        return "blocked_uncertain", "not_pending_in_segment_state", "not eligible in selected segment-state run"
    if not ES_OA_RE.search(text):
        return "blocked_uncertain", "missing_es_oa_token", "no ES_OA surface found"
    if RESIDUAL_RE.search(text):
        return "needs_es_oa_event_residual_repair", "visible_residual_or_mojibake", "visible residual, mojibake, or malformed agreement remains"
    if LOCAL_PLAYER_RE.search(text) and re.search(r"GetPlayer|ROOT\.Char|voc[eę]|você", text, re.IGNORECASE):
        return "needs_es_oa_event_local_player_policy", "local_player_or_second_person", "gender surface depends on player or second-person perspective"
    if TITLE_TRAIT_RE.search(haystack):
        return "needs_es_oa_event_title_trait_policy", "title_trait_role_entity", "title, role, or named entity semantics are mixed into the event"
    if OPTION_TOOLTIP_RE.search(key):
        return "needs_es_oa_event_option_tooltip_policy", "short_option_tooltip", "short event option/tooltip needs a narrow policy"
    if DOMAIN_RE.search(haystack):
        return "needs_es_oa_event_domain_context", "event_domain_context", "event-specific domain terms are needed before lifecycle"
    if ACTOR_RECIPIENT_RE.search(text):
        return "needs_es_oa_event_actor_recipient_policy", "actor_recipient_scope", "gender surface depends on actor/recipient/event scope"
    if TARGET_SCOPE_RE.search(text):
        return "needs_es_oa_event_target_gender_policy", "target_gender_scope", "gender surface depends on target or generic scope"
    if SUBJECT_OBJECT_RE.search(text):
        return "needs_es_oa_event_subject_object_composer", "subject_object_dynamic", "dynamic subject/object token affects agreement"
    if len(text) > 150 or DIALOGUE_LONGFORM_RE.search(haystack):
        return "needs_es_oa_event_longform_composer", "event_longform_dialogue", "longform event prose needs contextual composition"
    if can_false_reopen(state):
        return "es_oa_event_ready_false_reopen", "short_false_reopen_clear", "short event surface appears governable for future lifecycle"
    if state_is_pending_confirmed(state):
        return "es_oa_event_ready_lifecycle", "short_lifecycle_clear", "short event surface appears aligned for future lifecycle"
    return "needs_new_microagent", "uncategorized_event_es_oa", "event ES_OA pattern did not fit current policies"


def decide(row: dict[str, Any], state: dict[str, Any] | None) -> dict[str, Any]:
    decision, subpolicy, notes = classify(row, state)
    return {
        "segment_id": int(row["segment_id"]),
        "key": row["key"],
        "relative_path": row["relative_path"],
        "current_text": row["current_text"],
        "source_es_oa_decision": row["es_oa_decision"],
        "es_oa_event_decision": decision,
        "es_oa_event_subpolicy": subpolicy,
        "tokens_seen": tokens_seen(row["current_text"]),
        "requires_lifecycle_later": decision in {"es_oa_event_ready_false_reopen", "es_oa_event_ready_lifecycle"},
        "requires_apply_later": False,
        "corrected_text": "",
        "notes": notes,
    }


def output_paths() -> tuple[Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_dynamic_gender_es_oa_event_context_review"
    return base.with_suffix(".jsonl"), base.with_suffix(".txt")


def write_reports(rows: list[dict[str, Any]], segment_state_run_id: int) -> tuple[Path, Path, Counter[str], Counter[str]]:
    jsonl_path, txt_path = output_paths()
    decision_counts = Counter(row["es_oa_event_decision"] for row in rows)
    subpolicy_counts = Counter(row["es_oa_event_subpolicy"] for row in rows)
    ready_count = sum(1 for row in rows if row["requires_lifecycle_later"])
    apply_count = sum(1 for row in rows if row["requires_apply_later"])

    if ready_count >= 5:
        recommendation = "prepare_narrow_readonly_es_oa_event_lifecycle"
    elif decision_counts:
        top_decision, top_count = decision_counts.most_common(1)[0]
        recommendation = f"register_specific_microagent_or_policy_for_{top_decision}" if top_count >= 8 else "close_tree_and_migrate_fragmented_items_to_general_event_or_residual_review"
    else:
        recommendation = "blocked_no_rows"

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Dynamic gender ES_OA event context review",
        "",
        f"segment_state_run_id: {segment_state_run_id}",
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
        "source_es_oa_decision",
        "es_oa_event_decision",
        "es_oa_event_subpolicy",
        "tokens_seen",
        "requires_lifecycle_later",
        "requires_apply_later",
        "corrected_text",
        "notes",
    }
    for row in rows:
        missing = required - set(row)
        if missing:
            raise SystemExit(f"missing fields for {row.get('segment_id')}: {sorted(missing)}")
        if row["source_es_oa_decision"] != "needs_es_oa_event_context_composer":
            raise SystemExit(f"unexpected source decision for {row['segment_id']}: {row['source_es_oa_decision']}")
        if row["es_oa_event_decision"] not in ALLOWED_DECISIONS:
            raise SystemExit(f"unexpected event decision for {row['segment_id']}: {row['es_oa_event_decision']}")
        if row["requires_apply_later"] and not row["corrected_text"]:
            raise SystemExit(f"apply candidate without corrected_text: {row['segment_id']}")
        if row["corrected_text"] and TOKEN_RE.findall(row["current_text"]) != TOKEN_RE.findall(row["corrected_text"]):
            raise SystemExit(f"token mismatch in corrected_text: {row['segment_id']}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--es-oa-jsonl", required=True)
    parser.add_argument("--segment-state-run-id", type=int, required=True)
    args = parser.parse_args()

    source_rows = collect_source_rows(args.es_oa_jsonl)
    segment_ids = [int(row["segment_id"]) for row in source_rows]
    with connect_readonly() as conn:
        states = fetch_states(conn, args.segment_state_run_id, segment_ids)

    reviewed = [decide(row, states.get(int(row["segment_id"]))) for row in source_rows]
    validate_rows(reviewed)
    jsonl_path, txt_path, decision_counts, subpolicy_counts = write_reports(reviewed, args.segment_state_run_id)

    print(f"wrote_jsonl={jsonl_path}")
    print(f"wrote_txt={txt_path}")
    print(f"segment_state_run_id={args.segment_state_run_id}")
    print(f"total_reviewed={len(reviewed)}")
    print("decision_counts=" + json.dumps(dict(sorted(decision_counts.items())), ensure_ascii=False, sort_keys=True))
    print("subpolicy_counts=" + json.dumps(dict(sorted(subpolicy_counts.items())), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
