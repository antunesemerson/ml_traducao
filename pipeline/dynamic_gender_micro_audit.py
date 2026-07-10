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


RULE_VERSION = "dynamic_gender_micro_audit_v1"
EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76
EXPECTED_SUBLANE = "select_cstring_spanish_residue_candidate"

SELECT_CSTRING_RE = re.compile(r"Select_CString\([^)]*\)")
ES_HELPER_RE = re.compile(r"\.Custom\('ES_[A-Za-z0-9_]+'\)")
BRACKET_RE = re.compile(r"\[[^\]]+\]")
VARIABLE_RE = re.compile(r"\$[^$]+\$")
SPANISH_WORD_RE = re.compile(
    r"(?<![-\w])(?:muchos|muchas|poco|poca|buenos|buenas|acuerdo|ganas|gana|han|hab[eé]is|"
    r"tu personaje|su personaje|los campesinos|terrenos de caza)(?![-\w])",
    re.IGNORECASE,
)
PORTUGUESE_CLITIC_RE = re.compile(r"\b(?:mantê|servi|tratá|vê)-los\b", re.IGNORECASE)


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


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
                raise SystemExit(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def connect_readonly() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db.get_database_path(db.load_settings())}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def fetch_context(conn: sqlite3.Connection, segment_id: int):
    return conn.execute(
        """
        SELECT
            s.id AS segment_id,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.english_text,
            s.spanish_text,
            s.old_text,
            o.portuguese_text AS current_output_text,
            st.state_group,
            st.needs_human,
            st.active_action,
            st.candidate_action,
            st.policy_action,
            st.reasons_json
        FROM source_segments s
        JOIN output_segments o ON o.segment_id = s.id
        JOIN segment_state_items st ON st.segment_id = s.id AND st.run_id = ?
        WHERE s.id = ?
        """,
        (EXPECTED_SEGMENT_STATE_RUN_ID, segment_id),
    ).fetchone()


def token_inventory(value: str) -> dict[str, list[str]]:
    return {
        "select_cstring": SELECT_CSTRING_RE.findall(value),
        "es_helper": ES_HELPER_RE.findall(value),
        "brackets": BRACKET_RE.findall(value),
        "variables": VARIABLE_RE.findall(value),
    }


def spanish_hits(value: str) -> list[str]:
    masked = PORTUGUESE_CLITIC_RE.sub("", value)
    return sorted({match.group(0) for match in SPANISH_WORD_RE.finditer(masked)})


def simple_literal_repair(value: str) -> tuple[str, str]:
    replacements = [
        ("muchos más", "muito mais"),
        ("muchos mas", "muito mais"),
        ("poco", "pouco"),
        ("acuerdo", "acordo"),
    ]
    candidate = value
    notes: list[str] = []
    for old, new in replacements:
        if old in candidate:
            candidate = candidate.replace(old, new)
            notes.append(f"{old}->{new}")
    return candidate, "; ".join(notes)


def classify(row: dict[str, Any], context: sqlite3.Row) -> dict[str, Any]:
    current = str(context["current_output_text"] or "")
    candidate, repair_notes = simple_literal_repair(current)
    current_tokens = token_inventory(current)
    candidate_tokens = token_inventory(candidate)
    hits = spanish_hits(current)
    state_guard_ok = (
        context["state_group"] == "pending"
        and int(context["needs_human"] or 0) == 1
        and context["active_action"] == "needs_autofix"
    )
    token_integrity_ok = current_tokens == candidate_tokens

    decision = "audit_block_false_positive_no_literal_repair"
    safe_for_future_apply = False
    reason = "no unambiguous Spanish literal residue after masking valid Portuguese clitics"
    if row.get("es_helper") or ES_HELPER_RE.search(current):
        decision = "audit_block_context_required_es_helper"
        reason = "ES helper and gendered Select_CString payload require contextual policy"
    elif hits and repair_notes and candidate != current and token_integrity_ok:
        decision = "audit_accept_literal_repair_candidate"
        safe_for_future_apply = True
        reason = f"literal repair only: {repair_notes}"
    elif hits:
        decision = "audit_block_spanish_hint_without_safe_repair"
        reason = "Spanish hint remains but no approved literal-only repair was available"

    if not state_guard_ok:
        decision = "audit_block_state_guard"
        safe_for_future_apply = False
        reason = "segment state no longer matches expected pending needs_autofix guard"
    if not token_integrity_ok:
        decision = "audit_block_token_integrity"
        safe_for_future_apply = False
        reason = "candidate would alter protected dynamic token inventory"

    return {
        "segment_id": int(row["segment_id"]),
        "audit_key": RULE_VERSION,
        "source_diagnostic_sublane": row.get("sublane") or "",
        "relative_path": context["relative_path"],
        "source_key": context["source_key"],
        "source_line_number": int(context["source_line_number"]),
        "english_text": context["english_text"],
        "spanish_text": context["spanish_text"],
        "current_output_text": current,
        "candidate_text": candidate if safe_for_future_apply else current,
        "audit_decision": decision,
        "audit_reason": reason,
        "spanish_hits": hits,
        "repair_notes": repair_notes,
        "state_guard_ok": state_guard_ok,
        "token_integrity_ok": token_integrity_ok,
        "structure_integrity_ok": token_integrity_ok,
        "select_cstring_preserved": current_tokens["select_cstring"] == candidate_tokens["select_cstring"],
        "es_helper_preserved": current_tokens["es_helper"] == candidate_tokens["es_helper"],
        "safe_for_future_apply_batch": safe_for_future_apply,
        "requires_human_review": not safe_for_future_apply,
        "requires_apply_later": False,
        "requires_lifecycle_later": False,
        "false_safe_risk": False,
        "needs_context": decision in {"audit_block_context_required_es_helper", "audit_block_spanish_hint_without_safe_repair"},
        "state_markers": {
            "needs_human": int(context["needs_human"] or 0),
            "active_action": context["active_action"],
            "candidate_action": context["candidate_action"],
            "policy_action": context["policy_action"],
            "reasons_json": context["reasons_json"],
        },
    }


def build_summary(records: list[dict[str, Any]], diagnostic_summary: dict[str, Any]) -> dict[str, Any]:
    decisions = Counter(row["audit_decision"] for row in records)
    safe_count = sum(1 for row in records if row["safe_for_future_apply_batch"])
    context_count = sum(1 for row in records if row["needs_context"])
    return {
        "schema_version": 1,
        "source": RULE_VERSION,
        "input_diagnostic_source": diagnostic_summary.get("source", ""),
        "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
        "ledger_run_id": EXPECTED_LEDGER_RUN_ID,
        "audited_count": len(records),
        "safe_for_future_apply_batch_count": safe_count,
        "blocked_count": len(records) - safe_count,
        "needs_context_count": context_count,
        "decision_counts": dict(sorted(decisions.items())),
        "accepted_candidate_ids": [row["segment_id"] for row in records if row["safe_for_future_apply_batch"]],
        "blocked_candidate_ids": [row["segment_id"] for row in records if not row["safe_for_future_apply_batch"]],
        "false_safe_risk_count": sum(1 for row in records if row["false_safe_risk"]),
        "requires_lifecycle_later_count": sum(1 for row in records if row["requires_lifecycle_later"]),
        "apply_ready_now": 0,
        "production_full_recommended_now": False,
        "lifecycle_reindex_recommended_now": False,
        "recommended_next_action": (
            "protected_apply_dry_run_for_accepted"
            if safe_count
            else "hold_dynamic_gender_until_policy_design"
        ),
        "recommended_next_prompt": (
            "chat_exec_candidate_apply_plan_preview_prompt.md"
            if safe_count
            else "chat_exec_dynamic_gender_policy_design_prompt.md"
        ),
    }


def write_outputs(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_dynamic_gender_micro_audit"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "dynamic gender micro audit",
        f"audited_count={summary['audited_count']}",
        f"safe_for_future_apply_batch_count={summary['safe_for_future_apply_batch_count']}",
        f"blocked_count={summary['blocked_count']}",
        f"needs_context_count={summary['needs_context_count']}",
        f"decision_counts={json.dumps(summary['decision_counts'], ensure_ascii=False, sort_keys=True)}",
        f"accepted_candidate_ids={summary['accepted_candidate_ids']}",
        f"blocked_candidate_ids={summary['blocked_candidate_ids']}",
        "apply_ready_now=0",
        "production_full_recommended_now=false",
        "lifecycle_reindex_recommended_now=false",
        f"recommended_next_action={summary['recommended_next_action']}",
        f"recommended_next_prompt={summary['recommended_next_prompt']}",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--diagnostic-jsonl", required=True)
    parser.add_argument("--diagnostic-summary-json", required=True)
    args = parser.parse_args()
    diagnostic_summary = read_json(db.project_path(args.diagnostic_summary_json))
    diagnostic_rows = [
        row for row in read_jsonl(db.project_path(args.diagnostic_jsonl))
        if row.get("recommendation") == "build_read_only_micro_audit"
        and row.get("sublane") == EXPECTED_SUBLANE
    ]
    if len(diagnostic_rows) != int(diagnostic_summary.get("read_only_micro_audit_candidate_count") or 0):
        raise SystemExit("diagnostic candidate count guard failed")
    with connect_readonly() as conn:
        records = []
        for row in diagnostic_rows:
            context = fetch_context(conn, int(row["segment_id"]))
            if context is None:
                raise SystemExit(f"missing context for {row['segment_id']}")
            records.append(classify(row, context))
    summary = build_summary(records, diagnostic_summary)
    txt_path, jsonl_path, summary_path = write_outputs(records, summary)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    for key in [
        "audited_count",
        "safe_for_future_apply_batch_count",
        "blocked_count",
        "needs_context_count",
        "recommended_next_action",
        "recommended_next_prompt",
    ]:
        print(f"{key}={summary[key]}")


if __name__ == "__main__":
    main()
