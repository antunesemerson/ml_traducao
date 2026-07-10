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


AUDIT_KEY = "semantic_short_label_autofix_candidate_audit"
DISCOVERY_KEY = "semantic_short_label_autofix_candidate_discovery"
EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_CANDIDATES = 8

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

def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_semantic_short_label_autofix_candidate_audit"
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


def changed_fragments(original: str, candidate: str) -> list[tuple[str, str]]:
    known_pairs = [
        ("bueno", "bom"),
        ("acuerdo", "acordo"),
        ("eso", "isso"),
        ("muchos más", "muitos mais"),
        ("muchos mÃ¡s", "muitos mais"),
        ("poco", "pouco"),
        (" #!", " #!"),
        (" )", ")"),
    ]
    found: list[tuple[str, str]] = []
    for before, after in known_pairs:
        if before in original and after in candidate and original != candidate:
            found.append((before, after))
    if not found and original != candidate:
        found.append(("minor_spacing_or_punctuation", "minor_spacing_or_punctuation"))
    return found


def validates_small_objective_change(row: dict[str, Any]) -> bool:
    original = str(row.get("current_output_text") or "")
    candidate = str(row.get("candidate_text") or "")
    candidate_type = str(row.get("candidate_type") or "")
    if not candidate_type or original == candidate:
        return False
    if candidate_type == "candidate_spacing_punctuation_cleanup":
        return re.sub(r"\s+\)", ")", re.sub(r"\s+([,.;:!?])", r"\1", original)) == candidate
    pairs = changed_fragments(original, candidate)
    if len(pairs) != 1:
        return False
    before, after = pairs[0]
    if before == "minor_spacing_or_punctuation":
        return False
    return original.replace(before, after, 1) == candidate


def validate_input_summaries(discovery_summary: dict[str, Any], status_summary: dict[str, Any], segment_state_run_id: int) -> None:
    if segment_state_run_id != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("segment_state_run_id argument guard failed")
    required_discovery = {
        "segment_state_run_id": 400,
        "total_reviewed": 240,
        "candidate_count": 8,
        "candidate_semantic_minor_lexical_repair": 6,
        "candidate_spacing_punctuation_cleanup": 2,
        "false_safe_risk_count": 0,
        "requires_apply_later_count": 0,
        "requires_lifecycle_later_count": 0,
    }
    for key, expected in required_discovery.items():
        actual = int(discovery_summary.get(key) or 0)
        if actual != expected:
            raise SystemExit(f"discovery summary guard failed: {key} expected {expected}, got {actual}")
    required_status = {
        "segment_state_run_id": 400,
        "ledger_run_id": 76,
        "architecture_closed": True,
        "known_leftovers_count": 35,
        "true_unknown_count": 0,
        "resolver_reviewed_total": 716,
        "resolver_suggestion_candidates": 0,
        "semantic_discovery_candidates": 8,
        "apply_ready_now": 0,
    }
    for key, expected in required_status.items():
        actual = status_summary.get(key)
        if isinstance(expected, bool):
            if bool(actual) is not expected:
                raise SystemExit(f"status summary guard failed: {key}")
        elif int(actual or 0) != expected:
            raise SystemExit(f"status summary guard failed: {key} expected {expected}, got {actual}")


def select_candidates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates = [row for row in rows if str(row.get("decision") or "").startswith("candidate_")]
    candidates.sort(key=lambda row: int(row["segment_id"]))
    if len(candidates) != EXPECTED_CANDIDATES:
        raise SystemExit(f"candidate count guard failed: {len(candidates)}")
    if any(row.get("requires_apply_later") or row.get("requires_lifecycle_later") for row in candidates):
        raise SystemExit("candidate future flag guard failed")
    if any(row.get("false_safe_risk") for row in candidates):
        raise SystemExit("candidate false-safe guard failed")
    return candidates


def validate_pending(conn: sqlite3.Connection, candidates: list[dict[str, Any]], run_id: int) -> None:
    ids = [int(row["segment_id"]) for row in candidates]
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT segment_id, state_group, is_closed, needs_output_apply, confirmed_matches_output
        FROM segment_state_items
        WHERE run_id = ? AND segment_id IN ({placeholders})
        """,
        (run_id, *ids),
    ).fetchall()
    if len(rows) != len(ids):
        raise SystemExit(f"missing segment_state rows: expected {len(ids)}, got {len(rows)}")
    bad = [
        dict(row)
        for row in rows
        if row["state_group"] != "pending"
        or int(row["is_closed"] or 0) != 0
        or int(row["needs_output_apply"] or 0) != 0
        or int(row["confirmed_matches_output"] or 0) != 1
    ]
    if bad:
        raise SystemExit(f"pending/apply/match guard failed: {bad[:3]}")


def classify_candidate(row: dict[str, Any]) -> dict[str, Any]:
    original = str(row.get("current_output_text") or "")
    candidate = str(row.get("candidate_text") or "")
    candidate_type = str(row.get("candidate_type") or "")
    tokens_ok = token_integrity_ok(original, candidate)
    structure_ok = structure_integrity_ok(original, candidate)
    small_change = validates_small_objective_change(row)

    audit_decision = "audit_accept_safe_candidate"
    audit_reason = "minor objective lexical/spacing repair; protected tokens and structure preserved"
    semantic_confidence = "high"
    value_score = "medium" if candidate_type == "candidate_spacing_punctuation_cleanup" else "high"
    safe_for_future_apply_batch = True
    requires_human_review = False
    false_safe_risk = False

    if not tokens_ok or not structure_ok:
        audit_decision = "audit_reject_token_or_structure_risk"
        audit_reason = "candidate does not preserve token or structural inventory"
        semantic_confidence = "low"
        value_score = "low"
        safe_for_future_apply_batch = False
        requires_human_review = True
    elif not small_change:
        audit_decision = "audit_reject_uncertain_semantics"
        audit_reason = "change is not a recognized small objective lexical or spacing repair"
        semantic_confidence = "low"
        value_score = "low"
        safe_for_future_apply_batch = False
        requires_human_review = True
    elif candidate_type == "candidate_spacing_punctuation_cleanup":
        audit_reason = "spacing cleanup around punctuation/parenthesis with all CK3 tokens preserved"
    elif candidate_type == "candidate_semantic_minor_lexical_repair":
        audit_reason = "literal Spanish residue normalized to Portuguese without changing CK3 tokens"

    return {
        "segment_id": int(row["segment_id"]),
        "audit_key": AUDIT_KEY,
        "source_discovery": DISCOVERY_KEY,
        "candidate_type": candidate_type,
        "open_families": row.get("open_families") or [],
        "original_text": str(row.get("original_text") or ""),
        "current_output_text": original,
        "candidate_text": candidate,
        "audit_decision": audit_decision,
        "audit_reason": audit_reason,
        "token_integrity_ok": tokens_ok,
        "structure_integrity_ok": structure_ok,
        "semantic_confidence": semantic_confidence,
        "value_score": value_score,
        "safe_for_future_apply_batch": safe_for_future_apply_batch,
        "requires_human_review": requires_human_review,
        "requires_apply_later": False,
        "requires_lifecycle_later": False,
        "false_safe_risk": false_safe_risk,
        "notes": f"source_decision={row.get('decision')}; source_notes={row.get('notes') or ''}",
    }


def build_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    decisions = Counter(row["audit_decision"] for row in records)
    safe_ids = [row["segment_id"] for row in records if row["safe_for_future_apply_batch"]]
    rejected_ids = [row["segment_id"] for row in records if str(row["audit_decision"]).startswith("audit_reject")]
    dominant_type = Counter(row["candidate_type"] for row in records if row["safe_for_future_apply_batch"]).most_common(1)
    safe_count = len(safe_ids)
    summary = {
        "schema_version": 1,
        "source": "semantic_short_label_autofix_candidate_audit_v1",
        "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
        "total_candidates_audited": len(records),
        "audit_accept_safe_candidate": decisions.get("audit_accept_safe_candidate", 0),
        "audit_accept_needs_human_review": decisions.get("audit_accept_needs_human_review", 0),
        "audit_reject_uncertain_semantics": decisions.get("audit_reject_uncertain_semantics", 0),
        "audit_reject_token_or_structure_risk": decisions.get("audit_reject_token_or_structure_risk", 0),
        "audit_reject_low_value_style_only": decisions.get("audit_reject_low_value_style_only", 0),
        "audit_reclassify_guarded_no_apply": decisions.get("audit_reclassify_guarded_no_apply", 0),
        "safe_for_future_apply_batch_count": safe_count,
        "requires_human_review_count": sum(1 for row in records if row["requires_human_review"]),
        "token_integrity_ok_count": sum(1 for row in records if row["token_integrity_ok"]),
        "structure_integrity_ok_count": sum(1 for row in records if row["structure_integrity_ok"]),
        "false_safe_risk_count": sum(1 for row in records if row["false_safe_risk"]),
        "requires_apply_later_count": sum(1 for row in records if row["requires_apply_later"]),
        "requires_lifecycle_later_count": sum(1 for row in records if row["requires_lifecycle_later"]),
        "accepted_candidate_ids": safe_ids,
        "rejected_candidate_ids": rejected_ids,
        "dominant_safe_candidate_type": dominant_type[0][0] if dominant_type else "",
        "create_specific_resolver_now": safe_count >= 5,
        "expand_discovery_before_apply": True,
        "production_full_recommended_now": False,
        "network_update_recommended": False,
        "network_update_data_only_optional": safe_count >= 5,
        "recommended_next_prompt": "chat_exec_semantic_short_label_autofix_candidate_discovery_expand_prompt.md",
        "recommended_next_prompt_focus": (
            "expandir descoberta com foco no tipo aceito dominante antes de qualquer apply"
            if safe_count >= 5
            else "melhorar recall e criterios antes de qualquer apply"
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

    lines = [
        "semantic short_label autofix candidate audit",
        f"segment_state_run_id={summary['segment_state_run_id']}",
        f"total_candidates_audited={summary['total_candidates_audited']}",
        "",
        "metrics:",
        f"audit_accept_safe_candidate={summary['audit_accept_safe_candidate']}",
        f"audit_accept_needs_human_review={summary['audit_accept_needs_human_review']}",
        f"audit_reject_uncertain_semantics={summary['audit_reject_uncertain_semantics']}",
        f"audit_reject_token_or_structure_risk={summary['audit_reject_token_or_structure_risk']}",
        f"audit_reject_low_value_style_only={summary['audit_reject_low_value_style_only']}",
        f"audit_reclassify_guarded_no_apply={summary['audit_reclassify_guarded_no_apply']}",
        f"safe_for_future_apply_batch_count={summary['safe_for_future_apply_batch_count']}",
        f"requires_human_review_count={summary['requires_human_review_count']}",
        f"token_integrity_ok_count={summary['token_integrity_ok_count']}",
        f"structure_integrity_ok_count={summary['structure_integrity_ok_count']}",
        f"false_safe_risk_count={summary['false_safe_risk_count']}",
        f"requires_apply_later_count={summary['requires_apply_later_count']}",
        f"requires_lifecycle_later_count={summary['requires_lifecycle_later_count']}",
        f"accepted_candidate_ids={summary['accepted_candidate_ids']}",
        f"rejected_candidate_ids={summary['rejected_candidate_ids']}",
        "",
        "answers:",
        f"1. really_good_candidates={summary['safe_for_future_apply_batch_count']}",
        "2. future_apply_batch_possible=yes, but only after expanded discovery/audit; no apply now"
        if summary["safe_for_future_apply_batch_count"] >= 5
        else "2. future_apply_batch_possible=no, expand discovery first",
        f"3. most_promising_candidate_type={summary['dominant_safe_candidate_type'] or 'none'}",
        f"4. create_specific_resolver_now={str(summary['create_specific_resolver_now']).lower()}",
        f"5. expand_discovery_before_apply={str(summary['expand_discovery_before_apply']).lower()}",
        f"6. production_full_recommended_now={str(summary['production_full_recommended_now']).lower()}",
        f"7. network_update_recommended={str(summary['network_update_recommended']).lower()}; data_only_optional={str(summary['network_update_data_only_optional']).lower()}",
        "",
        f"recommended_next_prompt={summary['recommended_next_prompt']}",
        f"recommended_next_prompt_focus={summary['recommended_next_prompt_focus']}",
        "",
        "safety:",
        "read_only_sqlite_query_only=true",
        "requires_apply_later_count=0",
        "requires_lifecycle_later_count=0",
        "production_full_recommended_now=false",
        "source_output_mutation=none",
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

    discovery_jsonl = db.project_path(args.discovery_jsonl)
    discovery_summary_path = db.project_path(args.discovery_summary_json)
    status_summary_path = db.project_path(args.status_summary_json)
    for path in [discovery_jsonl, discovery_summary_path, status_summary_path]:
        if not path.exists():
            raise SystemExit(f"missing required input: {path}")

    discovery_summary = read_json(discovery_summary_path)
    status_summary = read_json(status_summary_path)
    validate_input_summaries(discovery_summary, status_summary, args.segment_state_run_id)

    candidates = select_candidates(read_jsonl(discovery_jsonl))
    conn = connect_readonly()
    try:
        validate_pending(conn, candidates, args.segment_state_run_id)
    finally:
        conn.close()

    records = [classify_candidate(row) for row in candidates]
    summary = build_summary(records)
    txt_path, jsonl_path, summary_path = write_outputs(records, summary)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"total_candidates_audited={summary['total_candidates_audited']}")
    print(f"safe_for_future_apply_batch_count={summary['safe_for_future_apply_batch_count']}")
    print(f"false_safe_risk_count={summary['false_safe_risk_count']}")
    print(f"recommended_next_prompt={summary['recommended_next_prompt']}")


if __name__ == "__main__":
    main()
