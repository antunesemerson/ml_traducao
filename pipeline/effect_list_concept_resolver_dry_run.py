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


RESOLVER_KEY = "effect_list_concept_resolver_dry_run"
SOURCE_POLICY = "effect_list_concept_policy"
EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76
EXPECTED_TOTAL = 17
REUSE_POLICIES = {
    "concept_requirement_policy",
    "effect_list_trait_accolade_policy",
    "effect_list_gender_local_player_policy",
    "effect_list_script_value_policy",
}

BRACKET_RE = re.compile(r"\[[^\]]+\]")
VARIABLE_RE = re.compile(r"\$[^$]+\$")
FORMATTING_TAG_RE = re.compile(r"#[A-Za-z][A-Za-z0-9_:.{};,|]*|#!|#P|#N|#D")
DYNAMIC_RE = re.compile(r"Custom\(|Select_CString|Concept\(|ScriptValue|GetTrait|ROOT\.|FROM\.|SCOPE\.|TARGET\.|\[[^\]]+\]|\$[^$]+\$")
SCOPE_RE = re.compile(r"ROOT\.|FROM\.|SCOPE\.|TARGET\.|Get[A-Za-z0-9_]+")


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_effect_list_concept_resolver_dry_run"
    summary = reports_dir / f"{stamp}_effect_list_concept_resolver_dry_run_summary.json"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), summary


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
    ids = [int(row["segment_id"]) for row in samples]
    if len(ids) != len(set(ids)):
        raise SystemExit("duplicate sample segment_id in effect-list concept review")
    return samples


def summary_row(rows: list[dict[str, Any]], label: str) -> dict[str, Any]:
    summaries = [row for row in rows if row.get("record_type") == "summary"]
    if len(summaries) != 1:
        raise SystemExit(f"expected exactly one summary in {label}, got {len(summaries)}")
    return summaries[0]


def fetch_texts(conn: sqlite3.Connection, segment_ids: list[int], run_id: int) -> dict[int, dict[str, Any]]:
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT s.segment_id, s.state_group, s.is_closed, s.needs_output_apply,
               s.confirmed_matches_output, src.old_text, out.portuguese_text AS output_text
        FROM segment_state_items s
        LEFT JOIN source_segments src ON src.id = s.segment_id
        LEFT JOIN output_segments out ON out.segment_id = s.segment_id
        WHERE s.run_id = ? AND s.segment_id IN ({placeholders})
        """,
        (run_id, *segment_ids),
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def validate_inputs(spec: dict[str, Any], samples: list[dict[str, Any]], registry: dict[str, Any], strategy: dict[str, Any], previous: dict[str, Any]) -> None:
    if spec.get("policy_id") != SOURCE_POLICY:
        raise SystemExit("spec policy_id guard failed")
    if int(spec.get("segment_state_run_id") or 0) != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("spec segment_state_run_id guard failed")
    if int(spec.get("ledger_run_id") or 0) != EXPECTED_LEDGER_RUN_ID:
        raise SystemExit("spec ledger_run_id guard failed")
    if len(samples) != EXPECTED_TOTAL:
        raise SystemExit(f"review_total guard failed: {len(samples)}")
    for row in samples:
        if not str(row.get("effect_concept_decision") or "").startswith("effect_concept_reuse_"):
            raise SystemExit(f"bad effect_concept_decision: {row.get('segment_id')}")
        if row.get("matched_registered_policy") not in REUSE_POLICIES:
            raise SystemExit(f"bad reuse policy: {row.get('segment_id')}")
        if row.get("requires_apply_later") is True or row.get("requires_lifecycle_later") is True:
            raise SystemExit(f"future flag guard failed: {row.get('segment_id')}")
    if str(registry.get("mode") or "") != "apply" or int(registry.get("reuse_registered_policies") or 0) != EXPECTED_TOTAL:
        raise SystemExit("registry summary guard failed")
    candidate = next((row for row in strategy.get("candidate_ranking", []) if row.get("policy_key") == SOURCE_POLICY), None)
    if not candidate or int(candidate.get("candidate_segment_count") or 0) != EXPECTED_TOTAL:
        raise SystemExit("strategy candidate guard failed")
    if int(previous.get("holy_site_concept_guard_failed_count") or 0) != 0 or int(previous.get("false_safe_risk_count") or 0) != 0:
        raise SystemExit("previous resolver guard failed")


def token_inventory(value: str) -> dict[str, list[str]]:
    return {
        "bracket_expressions": BRACKET_RE.findall(value),
        "variables": VARIABLE_RE.findall(value),
        "formatting_tags": FORMATTING_TAG_RE.findall(value),
        "dynamic_tokens": DYNAMIC_RE.findall(value),
        "scope_getters": SCOPE_RE.findall(value),
        "line_breaks": ["\\n"] if "\n" in value else [],
    }


def token_integrity_ok(value: str) -> bool:
    return value.count("[") == value.count("]") and value.count("$") % 2 == 0


def make_record(sample: dict[str, Any], text: dict[str, Any]) -> dict[str, Any]:
    original = str(text.get("old_text") or sample.get("old_text") or "")
    current = str(text.get("output_text") or sample.get("output_text") or "")
    proposed = current or original
    original_tokens = token_inventory(original)
    proposed_tokens = token_inventory(proposed)
    reuse_policy = str(sample.get("matched_registered_policy") or "")
    guards = {
        "state_pending": str(text.get("state_group") or "") == "pending" and int(text.get("is_closed") or 0) == 0,
        "needs_output_apply_zero": int(text.get("needs_output_apply") or 0) == 0,
        "confirmed_matches_output": int(text.get("confirmed_matches_output") or 0) == 1,
        "concept_marker_present": bool(sample.get("concept_markers")),
        "effect_list_marker_present": bool(sample.get("effect_list_markers")),
        "reuse_policy_guard_ok": reuse_policy in REUSE_POLICIES,
        "bracket_expressions_preserved": original_tokens["bracket_expressions"] == proposed_tokens["bracket_expressions"],
        "variables_preserved": original_tokens["variables"] == proposed_tokens["variables"],
        "formatting_tags_preserved": original_tokens["formatting_tags"] == proposed_tokens["formatting_tags"],
        "dynamic_tokens_preserved": original_tokens["dynamic_tokens"] == proposed_tokens["dynamic_tokens"],
        "scope_getters_preserved": original_tokens["scope_getters"] == proposed_tokens["scope_getters"],
        "line_breaks_preserved": original.count("\n") == proposed.count("\n"),
        "token_integrity_ok": token_integrity_ok(original) and token_integrity_ok(proposed),
    }
    guards["effect_concept_guard_ok"] = guards["concept_marker_present"] and guards["effect_list_marker_present"]
    if not guards["state_pending"] or not guards["needs_output_apply_zero"] or not guards["confirmed_matches_output"]:
        decision, notes, risk = "blocked_by_context", "state guard failed in selected run", True
    elif not guards["token_integrity_ok"] or not guards["bracket_expressions_preserved"] or not guards["variables_preserved"] or not guards["formatting_tags_preserved"]:
        decision, notes, risk = "blocked_by_token_integrity", "CK3 token/bracket/variable/tag guard failed", True
    elif not guards["effect_concept_guard_ok"] or not guards["reuse_policy_guard_ok"]:
        decision, notes, risk = "blocked_by_domain_ambiguity", "concept/effect-list/reuse evidence incomplete", True
    else:
        decision, notes, risk = "guarded_no_apply_reuse_policy", "registered reuse policy guards this effect-list concept; no safe textual change is proposed", False
    return {
        "segment_id": int(sample["segment_id"]),
        "resolver_key": RESOLVER_KEY,
        "source_policy": SOURCE_POLICY,
        "reuse_policy": reuse_policy,
        "original_text": original,
        "current_output_text": current,
        "proposed_text": proposed,
        "decision": decision,
        "guards": guards,
        "would_change_output": False,
        "requires_apply_later": False,
        "requires_lifecycle_later": False,
        "false_safe_risk": risk,
        "notes": notes,
    }


def build_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    decisions = Counter(record["decision"] for record in records)
    summary = {
        "resolver_key": RESOLVER_KEY,
        "source_policy": SOURCE_POLICY,
        "total_reviewed": len(records),
        "suggestion_candidates": decisions.get("suggestion_candidate_effect_concept_preserved", 0),
        "guarded_no_apply": decisions.get("guarded_no_apply_reuse_policy", 0),
        "blocked_by_domain_ambiguity": decisions.get("blocked_by_domain_ambiguity", 0),
        "blocked_by_context": decisions.get("blocked_by_context", 0),
        "blocked_by_token_integrity": decisions.get("blocked_by_token_integrity", 0),
        "blocked_no_safe_change": decisions.get("blocked_no_safe_change", 0),
        "would_change_output": sum(1 for r in records if r["would_change_output"]),
        "confirmed_matches_output": sum(1 for r in records if r["guards"].get("confirmed_matches_output")),
        "false_safe_risk_count": sum(1 for r in records if r["false_safe_risk"]),
        "requires_apply_later_count": sum(1 for r in records if r["requires_apply_later"]),
        "requires_lifecycle_later_count": sum(1 for r in records if r["requires_lifecycle_later"]),
        "effect_concept_guard_ok_count": sum(1 for r in records if r["guards"].get("effect_concept_guard_ok")),
        "effect_concept_guard_failed_count": sum(1 for r in records if not r["guards"].get("effect_concept_guard_ok")),
        "reuse_policy_guard_ok_count": sum(1 for r in records if r["guards"].get("reuse_policy_guard_ok")),
        "token_integrity_ok_count": sum(1 for r in records if r["guards"].get("token_integrity_ok")),
        "sample_ids": [int(r["segment_id"]) for r in records],
        "decision_counts": dict(sorted(decisions.items())),
        "next_prompt": "chat_exec_script_value_effect_resolver_dry_run_prompt.md",
    }
    if summary["effect_concept_guard_failed_count"] or summary["false_safe_risk_count"] or summary["requires_lifecycle_later_count"]:
        summary["next_prompt"] = "chat_exec_effect_list_concept_domain_audit_prompt.md"
    return summary


def write_outputs(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    txt_path, jsonl_path, summary_path = output_paths()
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "effect-list concept resolver dry-run",
        f"resolver_key={RESOLVER_KEY}",
        f"source_policy={SOURCE_POLICY}",
        "",
        *[f"{k}={summary[k]}" for k in [
            "total_reviewed", "suggestion_candidates", "guarded_no_apply",
            "blocked_by_domain_ambiguity", "blocked_by_context", "blocked_by_token_integrity",
            "blocked_no_safe_change", "would_change_output", "confirmed_matches_output",
            "false_safe_risk_count", "requires_apply_later_count", "requires_lifecycle_later_count",
            "effect_concept_guard_ok_count", "effect_concept_guard_failed_count",
            "reuse_policy_guard_ok_count", "token_integrity_ok_count"
        ]],
        "",
        "answers:",
        f"1. Segmentos revisados: {summary['total_reviewed']}.",
        f"2. suggestion_candidate: {summary['suggestion_candidates']}.",
        f"3. guarded_no_apply: {summary['guarded_no_apply']}.",
        f"4. Guard de concept/effect-list falhou: {'sim' if summary['effect_concept_guard_failed_count'] else 'nao'}.",
        f"5. Exige lifecycle/apply: lifecycle={summary['requires_lifecycle_later_count']}, apply={summary['requires_apply_later_count']}.",
        f"6. Risco false-safe: {summary['false_safe_risk_count']}.",
        "7. Apply futuro: ainda nao; este dry-run validou reuso/guard, mas nao gerou proposta de mudanca.",
        f"8. Proximo resolver dry-run recomendado: {summary['next_prompt']}.",
        "",
        "production_full_recommended=false",
        "network_update_now=false",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--effect-concept-jsonl", required=True, type=Path)
    parser.add_argument("--effect-concept-spec-json", required=True, type=Path)
    parser.add_argument("--registry-jsonl", required=True, type=Path)
    parser.add_argument("--strategy-plan-json", required=True, type=Path)
    parser.add_argument("--previous-resolver-summary-json", required=True, type=Path)
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    args = parser.parse_args()
    if args.segment_state_run_id != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("segment_state_run_id guard failed")
    rows = read_jsonl(args.effect_concept_jsonl)
    samples = sample_rows(rows)
    validate_inputs(
        read_json(args.effect_concept_spec_json),
        samples,
        summary_row(read_jsonl(args.registry_jsonl), "registry"),
        read_json(args.strategy_plan_json),
        read_json(args.previous_resolver_summary_json),
    )
    ids = [int(row["segment_id"]) for row in samples]
    with connect_readonly() as conn:
        text_by_id = fetch_texts(conn, ids, args.segment_state_run_id)
    if set(text_by_id) != set(ids):
        raise SystemExit("segment_state lookup guard failed")
    records = [make_record(row, text_by_id[int(row["segment_id"])]) for row in samples]
    summary = build_summary(records)
    txt, jsonl, summary_path = write_outputs(records, summary)
    print(f"txt: {txt}")
    print(f"jsonl: {jsonl}")
    print(f"summary: {summary_path}")
    for key in ["total_reviewed", "suggestion_candidates", "guarded_no_apply", "blocked_by_domain_ambiguity", "blocked_by_token_integrity", "effect_concept_guard_failed_count", "false_safe_risk_count", "requires_lifecycle_later_count"]:
        print(f"{key}: {summary[key]}")
    print(f"next_prompt: {summary['next_prompt']}")


if __name__ == "__main__":
    main()
