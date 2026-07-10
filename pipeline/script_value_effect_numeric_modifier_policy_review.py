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


SOURCE_POLICY = "script_value_effect_policy"
MICRO_POLICY = "script_value_effect_numeric_modifier_policy"
EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_TOTAL = 3

PERCENT_RE = re.compile(r"(?<![A-Za-z0-9_])[-+]?\d+(?:[.,]\d+)?%")
MODIFIER_RE = re.compile(r"\bmodificador(?:es)?\b", re.IGNORECASE)
BRACKET_RE = re.compile(r"\[[^\]]+\]")
VARIABLE_RE = re.compile(r"\$[^$]+\$")
FORMATTING_TAG_RE = re.compile(r"#[A-Za-z][A-Za-z0-9_:.{};,|]*|#!|#P|#N|#D")


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_script_value_effect_numeric_modifier_policy_review"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), reports_dir / f"{base.name}_spec.json"


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


def token_integrity_ok(original: str, current: str) -> bool:
    return (
        original.count("[") == original.count("]")
        and current.count("[") == current.count("]")
        and original.count("$") % 2 == 0
        and current.count("$") % 2 == 0
        and BRACKET_RE.findall(original) == BRACKET_RE.findall(current)
        and VARIABLE_RE.findall(original) == VARIABLE_RE.findall(current)
        and FORMATTING_TAG_RE.findall(original) == FORMATTING_TAG_RE.findall(current)
    )


def validate_inputs(
    audit_rows: list[dict[str, Any]],
    audit_summary: dict[str, Any],
    resolver_rows: list[dict[str, Any]],
    policy_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
    if int(audit_summary.get("total_audited") or 0) != EXPECTED_TOTAL:
        raise SystemExit("audit total guard failed")
    expected_zero = ["false_safe_risk_count", "requires_apply_later_count", "requires_lifecycle_later_count"]
    for key in expected_zero:
        if int(audit_summary.get(key) or 0) != 0:
            raise SystemExit(f"audit zero guard failed for {key}")
    if int(audit_summary.get("percent_modifier_requires_policy") or 0) != EXPECTED_TOTAL:
        raise SystemExit("percent modifier audit guard failed")

    selected = [row for row in audit_rows if row.get("audit_decision") == "percent_modifier_requires_policy"]
    if len(selected) != EXPECTED_TOTAL:
        raise SystemExit(f"selected audit count guard failed: {len(selected)}")
    resolver_by_id = {int(row["segment_id"]): row for row in resolver_rows}
    policy_samples = [row for row in policy_rows if row.get("record_type") == "sample_review"]
    policy_by_id = {int(row["segment_id"]): row for row in policy_samples}
    for row in selected:
        segment_id = int(row["segment_id"])
        if row.get("source_policy") != SOURCE_POLICY:
            raise SystemExit("audit source policy guard failed")
        if row.get("requires_apply_later") or row.get("requires_lifecycle_later") or row.get("false_safe_risk"):
            raise SystemExit("audit future flag guard failed")
        if segment_id not in resolver_by_id or segment_id not in policy_by_id:
            raise SystemExit(f"segment missing in resolver/policy review: {segment_id}")
        if resolver_by_id[segment_id].get("decision") != "blocked_by_numeric_or_modifier_guard":
            raise SystemExit("resolver decision guard failed")
    return selected, resolver_by_id, policy_by_id


def make_record(
    audit_row: dict[str, Any],
    resolver_row: dict[str, Any],
    policy_row: dict[str, Any],
    state_row: dict[str, Any],
) -> dict[str, Any]:
    original = str(audit_row.get("original_text") or resolver_row.get("original_text") or policy_row.get("old_text") or "")
    current = str(audit_row.get("current_output_text") or resolver_row.get("current_output_text") or policy_row.get("output_text") or "")
    percent_surface = ", ".join(PERCENT_RE.findall(original))
    modifier_surface = ", ".join(MODIFIER_RE.findall(original))
    state_ok = (
        str(state_row.get("state_group") or "") == "pending"
        and int(state_row.get("is_closed") or 0) == 0
        and int(state_row.get("needs_output_apply") or 0) == 0
        and int(state_row.get("confirmed_matches_output") or 0) == 1
    )
    token_ok = token_integrity_ok(original, current)
    if state_ok and token_ok and percent_surface and modifier_surface:
        decision = "numeric_modifier_terminal_guard_with_percent_policy"
        terminal_guard = True
        notes = "terminal percent/modifier guard; low-volume knowledge should stay cataloged under script_value_effect_policy"
    elif state_ok and token_ok and percent_surface:
        decision = "needs_numeric_modifier_script_value_context_policy"
        terminal_guard = False
        notes = "percent surface lacks explicit modifier wording and would need context"
    elif state_ok and token_ok:
        decision = "needs_numeric_modifier_domain_context_policy"
        terminal_guard = False
        notes = "numeric surface needs domain context"
    else:
        decision = "numeric_modifier_blocked_uncertain"
        terminal_guard = False
        notes = "state or token guard failed"

    return {
        "segment_id": int(audit_row["segment_id"]),
        "source_policy": SOURCE_POLICY,
        "micro_policy": MICRO_POLICY,
        "original_text": original,
        "current_output_text": current,
        "audit_decision": "percent_modifier_requires_policy",
        "numeric_modifier_decision": decision,
        "percent_surface": percent_surface,
        "modifier_surface": modifier_surface,
        "token_integrity_ok": token_ok,
        "terminal_guard": terminal_guard,
        "requires_apply_later": False,
        "requires_lifecycle_later": False,
        "false_safe_risk": False,
        "notes": notes,
    }


def build_spec(records: list[dict[str, Any]]) -> dict[str, Any]:
    decisions = Counter(row["numeric_modifier_decision"] for row in records)
    terminal_count = sum(1 for row in records if row["terminal_guard"])
    spec = {
        "schema_version": 1,
        "created_for": "read_only_micro_policy_review",
        "source_policy": SOURCE_POLICY,
        "micro_policy": MICRO_POLICY,
        "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
        "total_reviewed": len(records),
        "numeric_modifier_terminal_guard_with_percent_policy": decisions.get("numeric_modifier_terminal_guard_with_percent_policy", 0),
        "needs_numeric_modifier_script_value_context_policy": decisions.get("needs_numeric_modifier_script_value_context_policy", 0),
        "needs_numeric_modifier_domain_context_policy": decisions.get("needs_numeric_modifier_domain_context_policy", 0),
        "numeric_modifier_blocked_uncertain": decisions.get("numeric_modifier_blocked_uncertain", 0),
        "terminal_guard_count": terminal_count,
        "token_integrity_ok_count": sum(1 for row in records if row["token_integrity_ok"]),
        "requires_apply_later_count": sum(1 for row in records if row["requires_apply_later"]),
        "requires_lifecycle_later_count": sum(1 for row in records if row["requires_lifecycle_later"]),
        "false_safe_risk_count": sum(1 for row in records if row["false_safe_risk"]),
        "sample_ids": [int(row["segment_id"]) for row in records],
        "policy_decision": "catalog_under_script_value_effect_policy_do_not_register_isolated_component",
        "register_component_now": False,
        "reason_not_registered": "low_volume_3_segments_no_visual_architectural_gain",
        "next_prompt": "chat_exec_resolver_wave1_consolidated_diagnostic_prompt.md",
    }
    if terminal_count != len(records) or spec["false_safe_risk_count"]:
        spec["next_prompt"] = "chat_exec_script_value_numeric_modifier_context_review_prompt.md"
    return spec


def write_outputs(records: list[dict[str, Any]], spec: dict[str, Any]) -> tuple[Path, Path, Path]:
    txt_path, jsonl_path, spec_path = output_paths()
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    metric_keys = [
        "total_reviewed",
        "numeric_modifier_terminal_guard_with_percent_policy",
        "needs_numeric_modifier_script_value_context_policy",
        "needs_numeric_modifier_domain_context_policy",
        "numeric_modifier_blocked_uncertain",
        "terminal_guard_count",
        "token_integrity_ok_count",
        "requires_apply_later_count",
        "requires_lifecycle_later_count",
        "false_safe_risk_count",
    ]
    lines = [
        "script-value effect numeric/modifier micro-policy review",
        f"source_policy={SOURCE_POLICY}",
        f"micro_policy={MICRO_POLICY}",
        "",
        *[f"{key}={spec[key]}" for key in metric_keys],
        "",
        "decision:",
        "1. Os 3 casos terminalizam como guard de percentual/modificador.",
        "2. A spec deve ser catalogada como conhecimento dentro de script_value_effect_policy.",
        "3. Nao registrar componente isolado agora: baixa volumetria e nenhum ganho visual/arquitetural relevante.",
        "4. Nao ha apply futuro recomendado nesta etapa.",
        f"5. Proximo prompt recomendado: {spec['next_prompt']}.",
        "",
        "production_full_recommended=false",
        "network_update_now=false",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, spec_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit-jsonl", required=True, type=Path)
    parser.add_argument("--audit-summary-json", required=True, type=Path)
    parser.add_argument("--resolver-jsonl", required=True, type=Path)
    parser.add_argument("--policy-jsonl", required=True, type=Path)
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    args = parser.parse_args()
    if args.segment_state_run_id != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("segment_state_run_id argument guard failed")

    audit_rows = read_jsonl(args.audit_jsonl)
    audit_summary = read_json(args.audit_summary_json)
    resolver_rows = read_jsonl(args.resolver_jsonl)
    policy_rows = read_jsonl(args.policy_jsonl)
    selected, resolver_by_id, policy_by_id = validate_inputs(audit_rows, audit_summary, resolver_rows, policy_rows)

    segment_ids = [int(row["segment_id"]) for row in selected]
    with connect_readonly() as conn:
        state_by_id = fetch_state(conn, segment_ids, args.segment_state_run_id)
    missing = sorted(set(segment_ids) - set(state_by_id))
    if missing:
        raise SystemExit(f"missing segment_state rows: {missing}")

    records = [
        make_record(row, resolver_by_id[int(row["segment_id"])], policy_by_id[int(row["segment_id"])], state_by_id[int(row["segment_id"])])
        for row in selected
    ]
    spec = build_spec(records)
    if spec["total_reviewed"] != EXPECTED_TOTAL:
        raise SystemExit("total_reviewed output guard failed")
    if spec["token_integrity_ok_count"] != EXPECTED_TOTAL:
        raise SystemExit("token_integrity output guard failed")
    if spec["requires_apply_later_count"] or spec["requires_lifecycle_later_count"] or spec["false_safe_risk_count"]:
        raise SystemExit("zero-risk output guard failed")
    txt_path, jsonl_path, spec_path = write_outputs(records, spec)
    print(f"txt: {txt_path}")
    print(f"jsonl: {jsonl_path}")
    print(f"spec: {spec_path}")
    for key in [
        "total_reviewed",
        "numeric_modifier_terminal_guard_with_percent_policy",
        "terminal_guard_count",
        "token_integrity_ok_count",
        "requires_apply_later_count",
        "requires_lifecycle_later_count",
        "false_safe_risk_count",
        "register_component_now",
        "next_prompt",
    ]:
        print(f"{key}: {spec[key]}")


if __name__ == "__main__":
    main()
