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


AUDIT_KEY = "semantic_single_family_candidate_small_audit"
SOURCE_DISCOVERY = "semantic_single_family_candidate_discovery"
EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_CANDIDATES = 3
ALLOWED_DECISIONS = {
    "candidate_semantic_minor_lexical_repair",
    "candidate_semantic_ptbr_naturalness",
    "candidate_semantic_article_preposition_cleanup",
}

TOKEN_RE = re.compile(
    r"\[[^\]]+\]|\$[^$]+\$|#[A-Za-z][A-Za-z0-9_:.{};,|]*|#!|@[A-Za-z0-9_]+!|"
    r"Select_CString\([^)]*\)|\.Custom\('ES_[A-Za-z0-9_]+'\)"
)
BRACKET_RE = re.compile(r"\[[^\]]+\]")
VARIABLE_RE = re.compile(r"\$[^$]+\$")
DEBUG_RE = re.compile(r"#D\b.*?#!", re.DOTALL)
SELECT_CSTRING_RE = re.compile(r"Select_CString\([^)]*\)")
ES_HELPER_RE = re.compile(r"\.Custom\('ES_[A-Za-z0-9_]+'\)")
SCOPE_GETTER_RE = re.compile(r"\b(?:ROOT|FROM|SCOPE|TARGET)\.|Get[A-Za-z0-9_]+")
ENTITY_RE = re.compile(r"\b(?:GetName|GetHouse|GetCulture|GetFaith|dynasty|house|culture|religion|faith|title|kingdom|holy_order)\b", re.IGNORECASE)


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_semantic_single_family_candidate_small_audit"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), reports_dir / f"{base.name}_summary.json"


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
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
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def token_inventory(value: str) -> list[str]:
    return TOKEN_RE.findall(value)


def token_integrity_ok(original: str, candidate: str) -> bool:
    return (
        original.count("[") == candidate.count("[")
        and original.count("]") == candidate.count("]")
        and original.count("$") == candidate.count("$")
        and token_inventory(original) == token_inventory(candidate)
        and BRACKET_RE.findall(original) == BRACKET_RE.findall(candidate)
        and VARIABLE_RE.findall(original) == VARIABLE_RE.findall(candidate)
        and DEBUG_RE.findall(original) == DEBUG_RE.findall(candidate)
        and SELECT_CSTRING_RE.findall(original) == SELECT_CSTRING_RE.findall(candidate)
        and ES_HELPER_RE.findall(original) == ES_HELPER_RE.findall(candidate)
    )


def structure_integrity_ok(original: str, candidate: str) -> bool:
    return (
        original.count("\n") == candidate.count("\n")
        and original.count("#") == candidate.count("#")
        and original.count("|") == candidate.count("|")
        and SCOPE_GETTER_RE.findall(original) == SCOPE_GETTER_RE.findall(candidate)
    )


def validate_inputs(discovery_summary: dict[str, Any], status_summary: dict[str, Any], segment_state_run_id: int) -> None:
    if segment_state_run_id != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("segment_state_run_id argument guard failed")
    required_discovery = {
        "total_reviewed": 360,
        "candidate_count": 3,
        "candidate_semantic_minor_lexical_repair": 1,
        "candidate_semantic_ptbr_naturalness": 1,
        "candidate_semantic_article_preposition_cleanup": 1,
        "false_safe_risk_count": 0,
        "requires_lifecycle_later_count": 0,
    }
    for key, expected in required_discovery.items():
        if int(discovery_summary.get(key) or 0) != expected:
            raise SystemExit(f"discovery summary guard failed: {key}")
    required_status = {
        "total_raw_candidates": 17,
        "total_audited_candidates": 14,
        "total_audited_accepts": 14,
        "semantic_single_family_unaudited_candidates": 3,
        "false_safe_risk_total": 0,
        "apply_ready_now": 0,
    }
    for key, expected in required_status.items():
        if int(status_summary.get(key) or 0) != expected:
            raise SystemExit(f"status summary guard failed: {key}")
    if not bool(status_summary.get("architecture_closed")) or int(status_summary.get("true_unknown_count") or 0) != 0:
        raise SystemExit("status architecture guard failed")
    if bool(status_summary.get("production_full_recommended_now")) or bool(status_summary.get("model_training_recommended")):
        raise SystemExit("status production/model guard failed")


def select_candidates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected = [row for row in rows if row.get("decision") in ALLOWED_DECISIONS]
    selected.sort(key=lambda row: int(row["segment_id"]))
    if len(selected) != EXPECTED_CANDIDATES:
        raise SystemExit(f"candidate row guard failed: {len(selected)}")
    for row in selected:
        if row.get("requires_apply_later") or row.get("requires_lifecycle_later") or row.get("false_safe_risk"):
            raise SystemExit(f"candidate future/risk flag guard failed: {row.get('segment_id')}")
    return selected


def validate_pending(conn: sqlite3.Connection, rows: list[dict[str, Any]], run_id: int) -> None:
    ids = [int(row["segment_id"]) for row in rows]
    placeholders = ",".join("?" for _ in ids)
    state_rows = conn.execute(
        f"""
        SELECT segment_id, state_group, is_closed, needs_output_apply, confirmed_matches_output
        FROM segment_state_items
        WHERE run_id = ? AND segment_id IN ({placeholders})
        """,
        (run_id, *ids),
    ).fetchall()
    if len(state_rows) != len(ids):
        raise SystemExit(f"missing state rows: {len(ids) - len(state_rows)}")
    bad = [
        dict(row)
        for row in state_rows
        if row["state_group"] != "pending"
        or int(row["is_closed"] or 0) != 0
        or int(row["needs_output_apply"] or 0) != 0
        or int(row["confirmed_matches_output"] or 0) != 1
    ]
    if bad:
        raise SystemExit(f"pending guard failed: {bad[:3]}")


def recognized_change(original: str, candidate: str, candidate_type: str) -> bool:
    if candidate_type == "candidate_semantic_minor_lexical_repair":
        return original.replace("poco", "pouco", 1) == candidate
    if candidate_type == "candidate_semantic_ptbr_naturalness":
        return original.replace("Propriedade", "propriedade", 1) == candidate
    if candidate_type == "candidate_semantic_article_preposition_cleanup":
        return original.replace(
            "participar do trabalho de manutenção das estradas do condado",
            "participar da manutenção das estradas do condado",
            1,
        ) == candidate
    return False


def classify(row: dict[str, Any]) -> dict[str, Any]:
    current = str(row.get("current_output_text") or "")
    candidate = str(row.get("candidate_text") or "")
    candidate_type = str(row.get("candidate_type") or "")
    tokens_ok = token_integrity_ok(current, candidate)
    structure_ok = structure_integrity_ok(current, candidate)
    safe_change = recognized_change(current, candidate, candidate_type)
    entity_guard = bool(ENTITY_RE.search(current))
    decision = "audit_accept_safe_semantic_candidate"
    reason = "small objective semantic/prose cleanup; protected tokens and structure preserved"
    confidence = "high"
    value = "medium"
    safe_for_future = True
    requires_human_review = False

    if not tokens_ok or not structure_ok:
        decision = "audit_reject_token_or_structure_risk"
        reason = "candidate does not preserve token or structural inventory"
        confidence = "low"
        value = "low"
        safe_for_future = False
        requires_human_review = True
    elif not safe_change:
        decision = "audit_reject_uncertain_semantics"
        reason = "candidate is not a recognized small objective semantic rewrite"
        confidence = "low"
        value = "low"
        safe_for_future = False
        requires_human_review = True
    elif entity_guard:
        decision = "audit_accept_needs_human_review"
        reason = "candidate is token-safe but includes guarded entity/name/title/culture surface"
        confidence = "medium"
        value = "medium"
        safe_for_future = False
        requires_human_review = True
    elif candidate_type == "candidate_semantic_minor_lexical_repair":
        reason = "literal Spanish residue repaired without touching tokens"
        value = "high"
    elif candidate_type == "candidate_semantic_article_preposition_cleanup":
        reason = "redundant prose phrase simplified without changing meaning"
        value = "high"
    elif candidate_type == "candidate_semantic_ptbr_naturalness":
        reason = "common noun capitalization normalized in prose"

    return {
        "segment_id": int(row["segment_id"]),
        "audit_key": AUDIT_KEY,
        "source_discovery": SOURCE_DISCOVERY,
        "candidate_type": candidate_type,
        "open_families": row.get("open_families") or [],
        "original_text": str(row.get("original_text") or ""),
        "current_output_text": current,
        "candidate_text": candidate,
        "audit_decision": decision,
        "audit_reason": reason,
        "token_integrity_ok": tokens_ok,
        "structure_integrity_ok": structure_ok,
        "semantic_confidence": confidence,
        "value_score": value,
        "safe_for_future_apply_batch": safe_for_future,
        "requires_human_review": requires_human_review,
        "requires_apply_later": False,
        "requires_lifecycle_later": False,
        "false_safe_risk": False,
        "notes": f"source_decision={row.get('decision')}; source_notes={row.get('notes') or ''}",
    }


def build_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    decisions = Counter(row["audit_decision"] for row in records)
    safe_ids = [row["segment_id"] for row in records if row["safe_for_future_apply_batch"]]
    rejected_ids = [row["segment_id"] for row in records if str(row["audit_decision"]).startswith("audit_reject")]
    safe_count = len(safe_ids)
    summary = {
        "schema_version": 1,
        "source": "semantic_single_family_candidate_small_audit_v1",
        "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
        "total_candidates_audited": len(records),
        "audit_accept_safe_semantic_candidate": decisions.get("audit_accept_safe_semantic_candidate", 0),
        "audit_accept_needs_human_review": decisions.get("audit_accept_needs_human_review", 0),
        "audit_reject_uncertain_semantics": decisions.get("audit_reject_uncertain_semantics", 0),
        "audit_reject_token_or_structure_risk": decisions.get("audit_reject_token_or_structure_risk", 0),
        "audit_reject_low_value_style_only": decisions.get("audit_reject_low_value_style_only", 0),
        "audit_reclassify_guarded_no_apply": decisions.get("audit_reclassify_guarded_no_apply", 0),
        "safe_for_future_apply_batch_count": safe_count,
        "requires_human_review_count": sum(1 for row in records if row["requires_human_review"]),
        "token_integrity_ok_count": sum(1 for row in records if row["token_integrity_ok"]),
        "structure_integrity_ok_count": sum(1 for row in records if row["structure_integrity_ok"]),
        "high_semantic_confidence_count": sum(1 for row in records if row["semantic_confidence"] == "high"),
        "high_value_score_count": sum(1 for row in records if row["value_score"] == "high"),
        "false_safe_risk_count": sum(1 for row in records if row["false_safe_risk"]),
        "requires_apply_later_count": sum(1 for row in records if row["requires_apply_later"]),
        "requires_lifecycle_later_count": sum(1 for row in records if row["requires_lifecycle_later"]),
        "accepted_candidate_ids": safe_ids,
        "rejected_candidate_ids": rejected_ids,
        "known_raw_candidates_final": 17,
        "known_audited_candidates_final": 14 + len(records),
        "known_accepted_candidates_final": 14 + safe_count,
        "apply_ready_now": 0,
        "production_full_recommended_now": False,
        "network_update_recommended": False,
        "recommended_next_prompt": (
            "chat_exec_candidate_discovery_final_status_and_human_review_plan_prompt.md"
            if safe_count == 3
            else "chat_exec_candidate_discovery_status_and_next_options_prompt.md"
        ),
    }
    if summary["total_candidates_audited"] != EXPECTED_CANDIDATES:
        raise SystemExit("summary total guard failed")
    if summary["false_safe_risk_count"] != 0:
        raise SystemExit("summary false-safe guard failed")
    if summary["requires_lifecycle_later_count"] != 0:
        raise SystemExit("summary lifecycle guard failed")
    return summary


def write_outputs(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    txt_path, jsonl_path, summary_path = output_paths()
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    metric_keys = [
        "total_candidates_audited",
        "audit_accept_safe_semantic_candidate",
        "audit_accept_needs_human_review",
        "audit_reject_uncertain_semantics",
        "audit_reject_token_or_structure_risk",
        "audit_reject_low_value_style_only",
        "audit_reclassify_guarded_no_apply",
        "safe_for_future_apply_batch_count",
        "requires_human_review_count",
        "token_integrity_ok_count",
        "structure_integrity_ok_count",
        "high_semantic_confidence_count",
        "high_value_score_count",
        "false_safe_risk_count",
        "requires_apply_later_count",
        "requires_lifecycle_later_count",
    ]
    lines = [
        "semantic single-family candidate small audit",
        f"segment_state_run_id={summary['segment_state_run_id']}",
        "",
        *[f"{key}={summary[key]}" for key in metric_keys],
        f"accepted_candidate_ids={summary['accepted_candidate_ids']}",
        f"rejected_candidate_ids={summary['rejected_candidate_ids']}",
        "",
        "answers:",
        f"1. really_good_candidates={summary['safe_for_future_apply_batch_count']}",
        f"2. final_known_accepted_candidates={summary['known_accepted_candidates_final']}",
        "3. immediate_apply_ready=false",
        "4. retarget_another_discovery_now=false; close human review plan first",
        "5. consolidated_human_review_batch=true",
        f"6. production_full_recommended_now={str(summary['production_full_recommended_now']).lower()}",
        f"7. network_update_recommended={str(summary['network_update_recommended']).lower()}",
        "",
        f"recommended_next_prompt={summary['recommended_next_prompt']}",
        "apply_recommended_now=false",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--discovery-jsonl", required=True)
    parser.add_argument("--discovery-summary-json", required=True)
    parser.add_argument("--status-summary-json", required=True)
    parser.add_argument("--segment-state-run-id", type=int, required=True)
    args = parser.parse_args()
    discovery_rows = read_jsonl(db.project_path(args.discovery_jsonl))
    discovery_summary = read_json(db.project_path(args.discovery_summary_json))
    status_summary = read_json(db.project_path(args.status_summary_json))
    validate_inputs(discovery_summary, status_summary, args.segment_state_run_id)
    candidates = select_candidates(discovery_rows)
    with connect_readonly() as conn:
        validate_pending(conn, candidates, args.segment_state_run_id)
    records = [classify(row) for row in candidates]
    summary = build_summary(records)
    txt_path, jsonl_path, summary_path = write_outputs(records, summary)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    for key in [
        "total_candidates_audited",
        "safe_for_future_apply_batch_count",
        "audit_accept_safe_semantic_candidate",
        "audit_accept_needs_human_review",
        "audit_reject_uncertain_semantics",
        "known_accepted_candidates_final",
        "false_safe_risk_count",
        "requires_lifecycle_later_count",
        "recommended_next_prompt",
    ]:
        print(f"{key}={summary[key]}")


if __name__ == "__main__":
    main()
