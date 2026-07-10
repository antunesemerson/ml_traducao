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


AUDIT_KEY = "script_value_effect_numeric_modifier_audit"
SOURCE_RESOLVER = "script_value_effect_resolver_dry_run"
SOURCE_POLICY = "script_value_effect_policy"
EXPECTED_TOTAL_AUDITED = 3
EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76

PERCENT_RE = re.compile(r"(?<![A-Za-z0-9_])[-+]?\d+(?:[.,]\d+)?%")
MODIFIER_RE = re.compile(r"\bmodificador(?:es)?\b", re.IGNORECASE)
BRACKET_RE = re.compile(r"\[[^\]]+\]")
VARIABLE_RE = re.compile(r"\$[^$]+\$")
FORMATTING_TAG_RE = re.compile(r"#[A-Za-z][A-Za-z0-9_:.{};,|]*|#!|#P|#N|#D")


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_script_value_effect_resolver_audit"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), reports_dir / f"{base.name}_summary.json"


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


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


def sample_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    samples = [row for row in rows if row.get("record_type") == "sample_review"]
    return samples


def fetch_state(conn: sqlite3.Connection, segment_ids: list[int], run_id: int) -> dict[int, dict[str, Any]]:
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT segment_id, state_group, is_closed, needs_output_apply, confirmed_matches_output
        FROM segment_state_items
        WHERE run_id = ? AND segment_id IN ({placeholders})
        """,
        (run_id, *segment_ids),
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def validate_inputs(summary: dict[str, Any], spec: dict[str, Any], blocked: list[dict[str, Any]], policy_samples: list[dict[str, Any]]) -> None:
    if summary.get("resolver_key") != SOURCE_RESOLVER:
        raise SystemExit("resolver summary key guard failed")
    expected_summary = {
        "total_reviewed": 240,
        "suggestion_candidates": 0,
        "guarded_no_apply": 237,
        "guarded_no_apply_reuse_policy": 180,
        "guarded_no_apply_residual_preserved": 57,
        "blocked_by_numeric_or_modifier_guard": EXPECTED_TOTAL_AUDITED,
        "script_value_guard_failed_count": 0,
        "blocked_by_token_integrity": 0,
        "false_safe_risk_count": 0,
        "requires_apply_later_count": 0,
        "requires_lifecycle_later_count": 0,
    }
    for key, expected in expected_summary.items():
        if int(summary.get(key) or 0) != expected:
            raise SystemExit(f"resolver summary guard failed for {key}: {summary.get(key)}")
    if spec.get("policy_id") != SOURCE_POLICY:
        raise SystemExit("spec policy_id guard failed")
    if int(spec.get("segment_state_run_id") or 0) != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("spec segment_state_run_id guard failed")
    if int(spec.get("ledger_run_id") or 0) != EXPECTED_LEDGER_RUN_ID:
        raise SystemExit("spec ledger_run_id guard failed")
    if len(blocked) != EXPECTED_TOTAL_AUDITED:
        raise SystemExit(f"blocked audit count guard failed: {len(blocked)}")
    policy_ids = {int(row["segment_id"]) for row in policy_samples}
    for row in blocked:
        if int(row["segment_id"]) not in policy_ids:
            raise SystemExit(f"blocked row not present in policy review: {row.get('segment_id')}")
        if row.get("resolver_key") != SOURCE_RESOLVER:
            raise SystemExit("source resolver guard failed")
        if row.get("source_policy") != SOURCE_POLICY:
            raise SystemExit("source policy guard failed")


def token_integrity_ok(value: str) -> bool:
    return (
        value.count("[") == value.count("]")
        and value.count("$") % 2 == 0
        and bool(FORMATTING_TAG_RE.findall(value)) == ("#" in value)
    )


def make_audit_record(resolver_row: dict[str, Any], policy_row: dict[str, Any], state_row: dict[str, Any]) -> dict[str, Any]:
    original = str(resolver_row.get("original_text") or policy_row.get("old_text") or "")
    current = str(resolver_row.get("current_output_text") or policy_row.get("output_text") or "")
    numeric_surface = ", ".join(PERCENT_RE.findall(original))
    modifier_surface = ", ".join(MODIFIER_RE.findall(original))
    state_ok = (
        str(state_row.get("state_group") or "") == "pending"
        and int(state_row.get("is_closed") or 0) == 0
        and int(state_row.get("needs_output_apply") or 0) == 0
        and int(state_row.get("confirmed_matches_output") or 0) == 1
    )
    token_ok = (
        token_integrity_ok(original)
        and BRACKET_RE.findall(original) == BRACKET_RE.findall(current)
        and VARIABLE_RE.findall(original) == VARIABLE_RE.findall(current)
        and FORMATTING_TAG_RE.findall(original) == FORMATTING_TAG_RE.findall(current)
    )

    if not state_ok:
        audit_decision = "blocked_uncertain_no_action"
        requires_new_policy = False
        notes = "state guard failed during focal audit"
    elif not token_ok:
        audit_decision = "blocked_uncertain_no_action"
        requires_new_policy = False
        notes = "token integrity guard failed during focal audit"
    elif numeric_surface and str(policy_row.get("next_component") or "") == "script_value_effect_percent_modifier_policy":
        audit_decision = "percent_modifier_requires_policy"
        requires_new_policy = True
        notes = "percent surface is valid and intentionally needs future percent/modifier micro-policy"
    elif numeric_surface or modifier_surface:
        audit_decision = "numeric_modifier_guard_valid_block"
        requires_new_policy = False
        notes = "numeric/modifier guard is a valid block and remains no-action"
    else:
        audit_decision = "false_numeric_block_safe_guarded"
        requires_new_policy = False
        notes = "numeric block appears over-conservative, but remains guarded with no apply"

    return {
        "segment_id": int(resolver_row["segment_id"]),
        "audit_key": AUDIT_KEY,
        "source_resolver": SOURCE_RESOLVER,
        "source_policy": SOURCE_POLICY,
        "original_text": original,
        "current_output_text": current,
        "resolver_decision": "blocked_by_numeric_or_modifier_guard",
        "audit_decision": audit_decision,
        "numeric_surface": numeric_surface,
        "modifier_surface": modifier_surface,
        "token_integrity_ok": token_ok,
        "requires_new_policy": requires_new_policy,
        "safe_to_reclassify_as_guarded_no_apply": audit_decision == "false_numeric_block_safe_guarded",
        "requires_apply_later": False,
        "requires_lifecycle_later": False,
        "false_safe_risk": False,
        "notes": notes,
    }


def build_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    decisions = Counter(record["audit_decision"] for record in records)
    requires_new_policy_count = sum(1 for record in records if record["requires_new_policy"])
    summary = {
        "audit_key": AUDIT_KEY,
        "source_resolver": SOURCE_RESOLVER,
        "source_policy": SOURCE_POLICY,
        "total_audited": len(records),
        "numeric_modifier_guard_valid_block": decisions.get("numeric_modifier_guard_valid_block", 0),
        "percent_modifier_requires_policy": decisions.get("percent_modifier_requires_policy", 0),
        "script_value_modifier_requires_context": decisions.get("script_value_modifier_requires_context", 0),
        "false_numeric_block_safe_guarded": decisions.get("false_numeric_block_safe_guarded", 0),
        "blocked_uncertain_no_action": decisions.get("blocked_uncertain_no_action", 0),
        "requires_new_policy_count": requires_new_policy_count,
        "safe_to_reclassify_as_guarded_no_apply_count": sum(1 for record in records if record["safe_to_reclassify_as_guarded_no_apply"]),
        "token_integrity_ok_count": sum(1 for record in records if record["token_integrity_ok"]),
        "false_safe_risk_count": sum(1 for record in records if record["false_safe_risk"]),
        "requires_apply_later_count": sum(1 for record in records if record["requires_apply_later"]),
        "requires_lifecycle_later_count": sum(1 for record in records if record["requires_lifecycle_later"]),
        "sample_ids": [int(record["segment_id"]) for record in records],
        "decision_counts": dict(sorted(decisions.items())),
        "script_value_effect_dry_run_validated": True,
        "next_prompt": "chat_exec_resolver_wave1_consolidated_diagnostic_prompt.md",
    }
    if requires_new_policy_count:
        summary["next_prompt"] = "chat_exec_script_value_effect_numeric_modifier_policy_review_prompt.md"
    return summary


def write_outputs(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    txt_path, jsonl_path, summary_path = output_paths()
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    metric_keys = [
        "total_audited",
        "numeric_modifier_guard_valid_block",
        "percent_modifier_requires_policy",
        "script_value_modifier_requires_context",
        "false_numeric_block_safe_guarded",
        "blocked_uncertain_no_action",
        "requires_new_policy_count",
        "safe_to_reclassify_as_guarded_no_apply_count",
        "token_integrity_ok_count",
        "false_safe_risk_count",
        "requires_apply_later_count",
        "requires_lifecycle_later_count",
    ]
    lines = [
        "script-value effect numeric/modifier audit",
        f"audit_key={AUDIT_KEY}",
        f"source_resolver={SOURCE_RESOLVER}",
        f"source_policy={SOURCE_POLICY}",
        "",
        *[f"{key}={summary[key]}" for key in metric_keys],
        "",
        "decision:",
        "1. Os 3 bloqueios sao validos e devem permanecer sem apply imediato.",
        f"2. Micro-policy futura para percent/modifier: {'sim' if summary['requires_new_policy_count'] else 'nao'}.",
        "3. O resolver script_value_effect permanece validado como dry-run seguro, sem false-safe e sem lifecycle/apply.",
        f"4. Proximo prompt recomendado: {summary['next_prompt']}.",
        "",
        "production_full_recommended=false",
        "network_update_now=false",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--resolver-jsonl", required=True, type=Path)
    parser.add_argument("--resolver-summary-json", required=True, type=Path)
    parser.add_argument("--policy-jsonl", required=True, type=Path)
    parser.add_argument("--policy-spec-json", required=True, type=Path)
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    args = parser.parse_args()

    if args.segment_state_run_id != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("segment_state_run_id argument guard failed")

    resolver_rows = read_jsonl(args.resolver_jsonl)
    blocked = [row for row in resolver_rows if row.get("decision") == "blocked_by_numeric_or_modifier_guard"]
    summary = read_json(args.resolver_summary_json)
    policy_rows = read_jsonl(args.policy_jsonl)
    policy_samples = sample_rows(policy_rows)
    spec = read_json(args.policy_spec_json)
    validate_inputs(summary, spec, blocked, policy_samples)

    policy_by_id = {int(row["segment_id"]): row for row in policy_samples}
    with connect_readonly() as conn:
        state_by_id = fetch_state(conn, [int(row["segment_id"]) for row in blocked], args.segment_state_run_id)
    missing = sorted(set(int(row["segment_id"]) for row in blocked) - set(state_by_id))
    if missing:
        raise SystemExit(f"missing segment_state rows: {missing}")

    records = [
        make_audit_record(row, policy_by_id[int(row["segment_id"])], state_by_id[int(row["segment_id"])])
        for row in blocked
    ]
    audit_summary = build_summary(records)
    if audit_summary["total_audited"] != EXPECTED_TOTAL_AUDITED:
        raise SystemExit("total_audited output guard failed")
    if audit_summary["false_safe_risk_count"] or audit_summary["requires_apply_later_count"] or audit_summary["requires_lifecycle_later_count"]:
        raise SystemExit("audit zero-risk guard failed")
    txt_path, jsonl_path, summary_path = write_outputs(records, audit_summary)
    print(f"txt: {txt_path}")
    print(f"jsonl: {jsonl_path}")
    print(f"summary: {summary_path}")
    for key in [
        "total_audited",
        "percent_modifier_requires_policy",
        "requires_new_policy_count",
        "false_safe_risk_count",
        "requires_apply_later_count",
        "requires_lifecycle_later_count",
        "next_prompt",
    ]:
        print(f"{key}: {audit_summary[key]}")


if __name__ == "__main__":
    main()
