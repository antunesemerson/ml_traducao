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
    "es_oa_event_safe_spanish_residual_repair",
    "es_oa_event_safe_english_residual_repair",
    "es_oa_event_safe_ptbr_fluency_repair",
    "needs_es_oa_event_target_gender_policy",
    "needs_es_oa_event_local_player_policy",
    "needs_es_oa_event_context_composer",
    "needs_es_oa_event_title_trait_policy",
    "needs_es_oa_event_domain_context",
    "needs_semantic_review",
    "blocked_uncertain",
}

TOKEN_RE = re.compile(
    r"Select_CString|Custom\(|ES_(?:OA|XA|EA|ElLa|DelDela|AlAla)|\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_:.{};,|]+|#!",
    re.IGNORECASE,
)
BAD_ENCODING_RE = re.compile(r"\b(?:vocę|nă|năo|săo|estăo|entăo|serăo)\b", re.IGNORECASE)
WORD_QUESTION_RE = re.compile(r"\w\?\w")
ES_OA_RE = re.compile(r"Custom\(\s*['\"]ES_OA['\"]\s*\)|ES_OA", re.IGNORECASE)
LOCAL_PLAYER_RE = re.compile(r"GetPlayer|ROOT\.Char|voc(?:e|\\u00ea|\\u0119)|voce", re.IGNORECASE)
TARGET_RE = re.compile(r"\[(?:recipient|host|saved_devaraja|woo_actor|local_character|[^.\]]+)\.Custom\(\s*['\"]ES_OA['\"]\s*\)\]", re.IGNORECASE)
SUBJECT_RE = re.compile(r"GetSheHe|GetShortUIName|GetFirstName|GetName|GetUIName", re.IGNORECASE)
TITLE_TRAIT_RE = re.compile(r"devaraja|mandala|title|trait|accolade|heir|herdeir|governor|exam|examination", re.IGNORECASE)
DOMAIN_RE = re.compile(r"tournament|contest|hunt|travel|assimilation|decision|activity|roaming|yearly|interaction", re.IGNORECASE)
LONGFORM_RE = re.compile(r'"|\.desc|desc\.|dialog|intro|interaction|travel_option|yearly|fallback', re.IGNORECASE)


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
        if row.get("es_oa_event_decision") != "needs_es_oa_event_residual_repair":
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


def state_is_pending_confirmed(state: dict[str, Any] | None) -> bool:
    if not state:
        return False
    return (
        state.get("state_group") == "pending"
        and int(state.get("needs_output_apply") or 0) == 0
        and int(state.get("confirmed_matches_output") or 0) == 1
        and int(state.get("is_closed") or 0) == 0
    )


def tokens_seen(text: str) -> list[str]:
    labels: list[str] = []
    for label, pattern in [
        ("ES_OA", ES_OA_RE),
        ("LocalPlayer", LOCAL_PLAYER_RE),
        ("TargetScope", TARGET_RE),
        ("SubjectObject", SUBJECT_RE),
        ("CK3DynamicToken", re.compile(r"\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_:.{};,|]+|#!")),
    ]:
        if pattern.search(text):
            labels.append(label)
    return labels


def maybe_safe_repair(row: dict[str, Any]) -> tuple[str, str, str, str] | None:
    text = row["current_text"]
    if TOKEN_RE.search(text) and (LOCAL_PLAYER_RE.search(text) or TARGET_RE.search(text) or SUBJECT_RE.search(text)):
        return None
    if "#EMP No#!" in text:
        corrected = text.replace("#EMP No#!", "#EMP Nao#!")
        return (
            "es_oa_event_safe_spanish_residual_repair",
            "spanish_residual_short_safe",
            corrected,
            "short Spanish residual can be corrected without touching CK3 tokens",
        )
    return None


def classify(row: dict[str, Any], state: dict[str, Any] | None) -> tuple[str, str, str, str]:
    text = row["current_text"]
    haystack = " ".join([row["relative_path"], row["key"], text])

    if row.get("source_es_oa_decision") != "needs_es_oa_event_context_composer":
        return "blocked_uncertain", "unexpected_source_branch", "", "source branch is not event context composer"
    if not state_is_pending_confirmed(state):
        return "blocked_uncertain", "not_pending_in_segment_state", "", "not eligible in selected segment-state run"
    if not ES_OA_RE.search(text):
        return "blocked_uncertain", "missing_es_oa_token", "", "no ES_OA surface found"

    safe = maybe_safe_repair(row)
    if safe:
        return safe
    if LOCAL_PLAYER_RE.search(text):
        return (
            "needs_es_oa_event_local_player_policy",
            "local_player_or_second_person",
            "",
            "residual is tied to local-player or second-person agreement",
        )
    if TITLE_TRAIT_RE.search(haystack):
        return (
            "needs_es_oa_event_title_trait_policy",
            "title_trait_or_named_entity",
            "",
            "title, role, or named entity semantics prevent a mechanical residual repair",
        )
    if TARGET_RE.search(text):
        return (
            "needs_es_oa_event_target_gender_policy",
            "target_gender_scope",
            "",
            "residual is tied to a target or scoped character gender surface",
        )
    if BAD_ENCODING_RE.search(text) or WORD_QUESTION_RE.search(text):
        return (
            "needs_semantic_review",
            "encoding_or_semantic_residual",
            "",
            "bad encoding or semantic residue is visible but not safe as a mechanical repair",
        )
    if DOMAIN_RE.search(haystack) and not LONGFORM_RE.search(haystack):
        return (
            "needs_es_oa_event_domain_context",
            "event_domain_context",
            "",
            "event domain context is needed before repairing the residual",
        )
    if LONGFORM_RE.search(haystack) or len(text) > 150:
        return (
            "needs_es_oa_event_context_composer",
            "event_dialogue_or_longform",
            "",
            "event dialogue or longform prose needs contextual composition",
        )
    return "needs_semantic_review", "semantic_residual_unclear", "", "meaning cannot be guaranteed by a short repair"


def decide(row: dict[str, Any], state: dict[str, Any] | None) -> dict[str, Any]:
    decision, subpolicy, corrected_text, notes = classify(row, state)
    return {
        "segment_id": int(row["segment_id"]),
        "key": row["key"],
        "relative_path": row["relative_path"],
        "current_text": row["current_text"],
        "source_es_oa_event_decision": row["es_oa_event_decision"],
        "residual_decision": decision,
        "residual_subpolicy": subpolicy,
        "tokens_seen": tokens_seen(row["current_text"]),
        "requires_lifecycle_later": False,
        "requires_apply_later": decision.startswith("es_oa_event_safe_"),
        "corrected_text": corrected_text,
        "notes": notes,
    }


def output_paths() -> tuple[Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_dynamic_gender_es_oa_event_residual_review"
    return base.with_suffix(".jsonl"), base.with_suffix(".txt")


def write_reports(rows: list[dict[str, Any]], segment_state_run_id: int) -> tuple[Path, Path, Counter[str], Counter[str]]:
    jsonl_path, txt_path = output_paths()
    decision_counts = Counter(row["residual_decision"] for row in rows)
    subpolicy_counts = Counter(row["residual_subpolicy"] for row in rows)
    safe_count = sum(count for decision, count in decision_counts.items() if decision.startswith("es_oa_event_safe_"))

    if safe_count >= 5:
        recommendation = "prepare_separate_protected_apply_prompt"
    else:
        needs_counts = Counter({key: value for key, value in decision_counts.items() if key.startswith("needs_")})
        if needs_counts and needs_counts.most_common(1)[0][1] >= 5:
            recommendation = f"register_specific_microagent_or_policy_for_{needs_counts.most_common(1)[0][0]}"
        else:
            recommendation = "do_not_apply_now_close_tree_or_return_to_global_diagnostic"

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Dynamic gender ES_OA event residual review",
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
            f"future_apply_count: {safe_count}",
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
        "source_es_oa_event_decision",
        "residual_decision",
        "residual_subpolicy",
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
        if row["source_es_oa_event_decision"] != "needs_es_oa_event_residual_repair":
            raise SystemExit(f"unexpected source decision for {row['segment_id']}: {row['source_es_oa_event_decision']}")
        if row["residual_decision"] not in ALLOWED_DECISIONS:
            raise SystemExit(f"unexpected residual decision for {row['segment_id']}: {row['residual_decision']}")
        if row["requires_apply_later"] and not row["corrected_text"]:
            raise SystemExit(f"apply candidate without corrected_text: {row['segment_id']}")
        corrected = row["corrected_text"]
        if corrected:
            if BAD_ENCODING_RE.search(corrected):
                raise SystemExit(f"bad encoding marker in corrected_text: {row['segment_id']}")
            if WORD_QUESTION_RE.search(corrected):
                raise SystemExit(f"question mark inside word in corrected_text: {row['segment_id']}")
            if TOKEN_RE.findall(row["current_text"]) != TOKEN_RE.findall(corrected):
                raise SystemExit(f"token mismatch in corrected_text: {row['segment_id']}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--es-oa-event-jsonl", required=True)
    parser.add_argument("--segment-state-run-id", type=int, required=True)
    args = parser.parse_args()

    source_rows = collect_source_rows(args.es_oa_event_jsonl)
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
