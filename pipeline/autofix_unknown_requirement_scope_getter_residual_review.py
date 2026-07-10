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

ALLOWED_DECISIONS = {
    "requirement_scope_getter_safe_spanish_residual_repair",
    "requirement_scope_getter_safe_english_residual_repair",
    "requirement_scope_getter_safe_ptbr_fluency_repair",
    "needs_requirement_scope_getter_title_law_policy",
    "needs_requirement_scope_getter_artifact_activity_policy",
    "needs_requirement_scope_getter_trait_accolade_policy",
    "needs_requirement_scope_getter_actor_target_policy",
    "needs_requirement_scope_getter_semantic_review",
    "blocked_uncertain",
}

TOKEN_RE = re.compile(
    r"Select_CString|Custom\(|ES_(?:OA|XA|EA|ElLa|DelDela|AlAla)|\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_:.{};,|]+|#!",
    re.IGNORECASE,
)
BAD_ENCODING_RE = re.compile(r"\b(?:vocę|nă|năo|săo|estăo|entăo|serăo)\b", re.IGNORECASE)
WORD_QUESTION_RE = re.compile(r"\w\?\w")
GETTER_RE = re.compile(r"\[[^\]]*\.(?:Get|Is|Has)[^\]]*\]", re.IGNORECASE)
TITLE_LAW_RE = re.compile(r"war|wars|faction|county|de_jure|attacker|defender|indemnizaciones|bando|guerra", re.IGNORECASE)
ARTIFACT_ACTIVITY_RE = re.compile(r"hunt|travel|activity|prestamistas|oportunidad|amistad|city|province", re.IGNORECASE)
TRAIT_RE = re.compile(r"trait|modifier|accolade|knight|skill", re.IGNORECASE)
ACTOR_TARGET_RE = re.compile(r"\[(?:recipient|target|attacker|defender|pertinent|city_province)[.\]]", re.IGNORECASE)


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
        if row.get("requirement_scope_getter_decision") != "needs_requirement_scope_getter_residual_repair":
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
        SELECT segment_id, state_group, needs_output_apply,
               confirmed_matches_output, is_closed
        FROM segment_state_items
        WHERE run_id = ?
          AND segment_id IN ({placeholders})
        """,
        (run_id, *segment_ids),
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def fetch_family_counts(conn: sqlite3.Connection, segment_ids: list[int]) -> dict[int, tuple[int, int, int]]:
    if not segment_ids:
        return {}
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT segment_id,
               COUNT(*) AS open_count,
               SUM(CASE WHEN issue_family = ? THEN 1 ELSE 0 END) AS target_count,
               SUM(CASE WHEN issue_family != ? THEN 1 ELSE 0 END) AS other_count
        FROM ml_issue_ledger_items
        WHERE run_id = 76
          AND status = 'open'
          AND segment_id IN ({placeholders})
        GROUP BY segment_id
        """,
        (TARGET_FAMILY, TARGET_FAMILY, *segment_ids),
    ).fetchall()
    return {
        int(row["segment_id"]): (int(row["open_count"]), int(row["target_count"] or 0), int(row["other_count"] or 0))
        for row in rows
    }


def state_is_pending_confirmed(state: dict[str, Any] | None) -> bool:
    return bool(
        state
        and state.get("state_group") == "pending"
        and int(state.get("needs_output_apply") or 0) == 0
        and int(state.get("confirmed_matches_output") or 0) == 1
        and int(state.get("is_closed") or 0) == 0
    )


def family_is_exact(family_counts: tuple[int, int, int] | None) -> bool:
    return family_counts == (1, 1, 0)


def tokens_seen(text: str) -> list[str]:
    labels: list[str] = []
    for label, pattern in [
        ("Getter", GETTER_RE),
        ("TitleLaw", TITLE_LAW_RE),
        ("ArtifactActivity", ARTIFACT_ACTIVITY_RE),
        ("ActorTarget", ACTOR_TARGET_RE),
        ("CK3DynamicToken", re.compile(r"\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_:.{};,|]+|#!")),
    ]:
        if pattern.search(text):
            labels.append(label)
    return labels


def classify(row: dict[str, Any], state: dict[str, Any] | None, family_counts: tuple[int, int, int] | None) -> tuple[str, str, str]:
    text = row["current_text"]
    haystack = " ".join([row["relative_path"], row["key"], text])

    if row.get("requirement_scope_getter_decision") != "needs_requirement_scope_getter_residual_repair":
        return "blocked_uncertain", "unexpected_source_branch", "source row is not needs_requirement_scope_getter_residual_repair"
    if not state_is_pending_confirmed(state):
        return "blocked_uncertain", "not_pending_in_segment_state", "not eligible in selected segment-state run"
    if not family_is_exact(family_counts):
        return "blocked_uncertain", "not_single_autofix_family", "ledger no longer has exactly one autofix_unknown open family"
    if text.count("[") != text.count("]") or text.count("$") % 2 != 0:
        return "needs_requirement_scope_getter_semantic_review", "broken_token_boundary", "token boundary prevents safe residual repair"
    if TITLE_LAW_RE.search(haystack):
        return "needs_requirement_scope_getter_title_law_policy", "title_law_residual_scope_getter", "residual depends on war/title-law domain and scoped character"
    if ARTIFACT_ACTIVITY_RE.search(haystack):
        return "needs_requirement_scope_getter_artifact_activity_policy", "artifact_activity_residual_scope_getter", "residual depends on activity/travel context and scoped getter"
    if TRAIT_RE.search(haystack):
        return "needs_requirement_scope_getter_trait_accolade_policy", "trait_accolade_residual_scope_getter", "residual depends on trait/accolade context"
    if ACTOR_TARGET_RE.search(text):
        return "needs_requirement_scope_getter_actor_target_policy", "actor_target_residual_scope_getter", "residual depends on actor/target/scope"
    return "needs_requirement_scope_getter_semantic_review", "semantic_residual_scope_getter", "meaning cannot be guaranteed by a mechanical repair"


def decide(row: dict[str, Any], state: dict[str, Any] | None, family_counts: tuple[int, int, int] | None) -> dict[str, Any]:
    decision, subpolicy, notes = classify(row, state, family_counts)
    return {
        "segment_id": int(row["segment_id"]),
        "key": row["key"],
        "relative_path": row["relative_path"],
        "current_text": row["current_text"],
        "source_requirement_scope_getter_decision": row["requirement_scope_getter_decision"],
        "residual_decision": decision,
        "residual_subpolicy": subpolicy,
        "tokens_seen": tokens_seen(row["current_text"]),
        "requires_lifecycle_later": False,
        "requires_apply_later": decision.startswith("requirement_scope_getter_safe_"),
        "corrected_text": "",
        "notes": notes,
    }


def output_paths() -> tuple[Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_autofix_unknown_requirement_scope_getter_residual_review"
    return base.with_suffix(".jsonl"), base.with_suffix(".txt")


def write_reports(rows: list[dict[str, Any]]) -> tuple[Path, Path, Counter[str], Counter[str]]:
    jsonl_path, txt_path = output_paths()
    decision_counts = Counter(row["residual_decision"] for row in rows)
    subpolicy_counts = Counter(row["residual_subpolicy"] for row in rows)
    safe_count = sum(count for decision, count in decision_counts.items() if decision.startswith("requirement_scope_getter_safe_"))

    if safe_count == 5:
        recommendation = "prepare_separate_protected_apply_prompt"
    elif safe_count < 5:
        needs_counts = Counter({key: value for key, value in decision_counts.items() if key.startswith("needs_")})
        if needs_counts and needs_counts.most_common(1)[0][1] >= 3:
            recommendation = f"register_specific_policy_or_microagent_for_{needs_counts.most_common(1)[0][0]}"
        else:
            recommendation = "do_not_apply_now_close_track_or_return_to_global_diagnostic"
    else:
        recommendation = "blocked_uncertain"

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Autofix unknown requirement scope/getter residual review",
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
        "source_requirement_scope_getter_decision",
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
        if row["source_requirement_scope_getter_decision"] != "needs_requirement_scope_getter_residual_repair":
            raise SystemExit(f"unexpected source decision for {row['segment_id']}: {row['source_requirement_scope_getter_decision']}")
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
    parser.add_argument("--consolidated-jsonl", required=True)
    parser.add_argument("--segment-state-run-id", type=int, required=True)
    args = parser.parse_args()

    source_rows = collect_source_rows(args.consolidated_jsonl)
    segment_ids = [int(row["segment_id"]) for row in source_rows]
    with connect_readonly() as conn:
        states = fetch_states(conn, args.segment_state_run_id, segment_ids)
        family_counts = fetch_family_counts(conn, segment_ids)

    reviewed = [
        decide(row, states.get(int(row["segment_id"])), family_counts.get(int(row["segment_id"])))
        for row in source_rows
    ]
    validate_rows(reviewed)
    jsonl_path, txt_path, decision_counts, subpolicy_counts = write_reports(reviewed)

    print(f"wrote_jsonl={jsonl_path}")
    print(f"wrote_txt={txt_path}")
    print(f"total_reviewed={len(reviewed)}")
    print("decision_counts=" + json.dumps(dict(sorted(decision_counts.items())), ensure_ascii=False, sort_keys=True))
    print("subpolicy_counts=" + json.dumps(dict(sorted(subpolicy_counts.items())), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
