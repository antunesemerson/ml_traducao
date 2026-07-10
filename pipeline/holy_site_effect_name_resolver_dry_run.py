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


RESOLVER_KEY = "holy_site_effect_name_resolver_dry_run"
SOURCE_POLICY = "holy_site_effect_name_policy"
REUSE_POLICY = "effect_list_concept_policy"
EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76
EXPECTED_TOTAL = 240
EXPECTED_DECISION = "holy_site_reuse_effect_list_concept_policy"

BRACKET_RE = re.compile(r"\[[^\]]+\]")
VARIABLE_RE = re.compile(r"\$[^$]+\$")
HOLY_SITE_VAR_RE = re.compile(r"\$holy_site_[^$]+_name\$")
FORMATTING_TAG_RE = re.compile(r"#[A-Za-z][A-Za-z0-9_:.{};,|]*|#!|#P|#N|#D")
DYNAMIC_RE = re.compile(r"Custom\(|Select_CString|Concept\(|ScriptValue|GetTrait|ROOT\.|FROM\.|SCOPE\.|TARGET\.")
SCOPE_RE = re.compile(r"ROOT\.|FROM\.|SCOPE\.|TARGET\.")

ALLOWED_DECISIONS = {
    "suggestion_candidate_holy_site_concept_preserved",
    "guarded_no_apply_reuse_effect_list_concept",
    "blocked_by_domain_ambiguity",
    "blocked_by_context",
    "blocked_by_token_integrity",
    "blocked_no_safe_change",
}


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_holy_site_effect_name_resolver_dry_run"
    summary = reports_dir / f"{stamp}_holy_site_effect_name_resolver_dry_run_summary.json"
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
        raise SystemExit("duplicate sample segment_id in holy-site review")
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
        SELECT
            s.segment_id,
            s.state_group,
            s.is_closed,
            s.needs_output_apply,
            s.confirmed_matches_output,
            src.old_text,
            src.spanish_text,
            src.english_text,
            out.portuguese_text AS output_text,
            (
              SELECT sc.confirmed_text
              FROM segment_confirmations sc
              WHERE sc.segment_id = s.segment_id
              ORDER BY sc.updated_at DESC, sc.id DESC
              LIMIT 1
            ) AS confirmed_text
        FROM segment_state_items s
        LEFT JOIN source_segments src ON src.id = s.segment_id
        LEFT JOIN output_segments out ON out.segment_id = s.segment_id
        WHERE s.run_id = ?
          AND s.segment_id IN ({placeholders})
        """,
        (run_id, *segment_ids),
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def validate_inputs(
    *,
    spec: dict[str, Any],
    review_samples: list[dict[str, Any]],
    registry_summary: dict[str, Any],
    strategy: dict[str, Any],
    previous_summary: dict[str, Any],
) -> None:
    if spec.get("policy_id") != SOURCE_POLICY:
        raise SystemExit(f"spec policy_id guard failed: {spec.get('policy_id')}")
    if int(spec.get("segment_state_run_id") or 0) != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("spec segment_state_run_id guard failed")
    if int(spec.get("ledger_run_id") or 0) != EXPECTED_LEDGER_RUN_ID:
        raise SystemExit("spec ledger_run_id guard failed")
    if len(review_samples) != EXPECTED_TOTAL:
        raise SystemExit(f"review_total guard failed: {len(review_samples)} expected {EXPECTED_TOTAL}")
    bad_decisions = [row["segment_id"] for row in review_samples if row.get("holy_site_decision") != EXPECTED_DECISION]
    if bad_decisions:
        raise SystemExit(f"holy_site decision guard failed: {bad_decisions[:5]}")
    if sum(1 for row in review_samples if row.get("requires_apply_later") is True):
        raise SystemExit("review requires_apply_later guard failed")
    if sum(1 for row in review_samples if row.get("requires_lifecycle_later") is True):
        raise SystemExit("review requires_lifecycle_later guard failed")
    if str(registry_summary.get("mode") or "") != "apply":
        raise SystemExit("registry apply mode guard failed")
    if int(registry_summary.get("review_total") or 0) != EXPECTED_TOTAL:
        raise SystemExit("registry review_total guard failed")
    if int(registry_summary.get("holy_site_reuse_effect_list_concept_policy") or 0) != EXPECTED_TOTAL:
        raise SystemExit("registry reuse coverage guard failed")
    ranking = strategy.get("candidate_ranking") or []
    candidate = next((row for row in ranking if row.get("policy_key") == SOURCE_POLICY), None)
    if not candidate or int(candidate.get("candidate_segment_count") or 0) != EXPECTED_TOTAL:
        raise SystemExit("strategy candidate guard failed")
    if int(previous_summary.get("title_dynasty_house_guard_failed_count") or 0) != 0:
        raise SystemExit("previous resolver domain guard failed")
    if int(previous_summary.get("false_safe_risk_count") or 0) != 0:
        raise SystemExit("previous resolver false-safe guard failed")


def token_inventory(value: str) -> dict[str, list[str]]:
    return {
        "bracket_expressions": BRACKET_RE.findall(value),
        "variables": VARIABLE_RE.findall(value),
        "holy_site_name_variables": HOLY_SITE_VAR_RE.findall(value),
        "formatting_tags": FORMATTING_TAG_RE.findall(value),
        "dynamic_tokens": DYNAMIC_RE.findall(value),
        "scope_getters": SCOPE_RE.findall(value),
        "line_breaks": ["\\n"] if "\n" in value else [],
    }


def token_integrity_ok(value: str) -> bool:
    if value.count("[") != value.count("]"):
        return False
    if value.count("$") % 2 != 0:
        return False
    if "#weak" in value and "#!" not in value:
        return False
    return True


def make_record(sample: dict[str, Any], text: dict[str, Any]) -> dict[str, Any]:
    segment_id = int(sample["segment_id"])
    original_text = str(text.get("old_text") or sample.get("old_text") or "")
    current_output = str(text.get("output_text") or sample.get("output_text") or "")
    proposed = current_output or original_text
    original_tokens = token_inventory(original_text)
    proposed_tokens = token_inventory(proposed)

    guards = {
        "state_pending": str(text.get("state_group") or "") == "pending" and int(text.get("is_closed") or 0) == 0,
        "needs_output_apply_zero": int(text.get("needs_output_apply") or 0) == 0,
        "confirmed_matches_output": int(text.get("confirmed_matches_output") or 0) == 1,
        "holy_site_marker_present": bool(sample.get("holy_site_markers")),
        "religion_marker_present": bool(sample.get("religion_markers")),
        "effect_marker_present": bool(sample.get("effect_markers")),
        "name_location_marker_present": bool(sample.get("name_location_markers")),
        "reuse_policy_guard_ok": sample.get("matched_registered_policy") == REUSE_POLICY or sample.get("matched_catalog_spec") == REUSE_POLICY,
        "holy_site_name_variable_present": bool(original_tokens["holy_site_name_variables"]),
        "bracket_expressions_preserved": original_tokens["bracket_expressions"] == proposed_tokens["bracket_expressions"],
        "variables_preserved": original_tokens["variables"] == proposed_tokens["variables"],
        "formatting_tags_preserved": original_tokens["formatting_tags"] == proposed_tokens["formatting_tags"],
        "dynamic_tokens_preserved": original_tokens["dynamic_tokens"] == proposed_tokens["dynamic_tokens"],
        "scope_getters_preserved": original_tokens["scope_getters"] == proposed_tokens["scope_getters"],
        "line_breaks_preserved": original_text.count("\n") == proposed.count("\n"),
        "token_integrity_ok": token_integrity_ok(original_text) and token_integrity_ok(proposed),
    }
    guards["holy_site_concept_guard_ok"] = (
        guards["holy_site_marker_present"]
        and guards["religion_marker_present"]
        and guards["effect_marker_present"]
        and guards["name_location_marker_present"]
        and guards["holy_site_name_variable_present"]
    )

    if not guards["state_pending"] or not guards["needs_output_apply_zero"] or not guards["confirmed_matches_output"]:
        decision = "blocked_by_context"
        notes = "state guard failed in selected run"
        false_safe = True
    elif not guards["token_integrity_ok"] or not guards["bracket_expressions_preserved"] or not guards["variables_preserved"] or not guards["formatting_tags_preserved"]:
        decision = "blocked_by_token_integrity"
        notes = "CK3 token, bracket, variable or formatting tag guard failed"
        false_safe = True
    elif not guards["holy_site_concept_guard_ok"] or not guards["reuse_policy_guard_ok"]:
        decision = "blocked_by_domain_ambiguity"
        notes = "holy-site/concept/reuse evidence is incomplete"
        false_safe = True
    else:
        decision = "guarded_no_apply_reuse_effect_list_concept"
        notes = "holy-site effect name reuses effect_list_concept_policy; no safe textual change is proposed in dry-run"
        false_safe = False

    would_change = False
    if decision == "suggestion_candidate_holy_site_concept_preserved":
        would_change = proposed != current_output
    if decision not in ALLOWED_DECISIONS:
        raise SystemExit(f"invalid resolver decision for {segment_id}: {decision}")
    return {
        "segment_id": segment_id,
        "resolver_key": RESOLVER_KEY,
        "source_policy": SOURCE_POLICY,
        "reuse_policy": REUSE_POLICY,
        "original_text": original_text,
        "current_output_text": current_output,
        "proposed_text": proposed,
        "decision": decision,
        "guards": guards,
        "would_change_output": would_change,
        "requires_apply_later": False,
        "requires_lifecycle_later": False,
        "false_safe_risk": false_safe,
        "notes": notes,
    }


def build_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    decisions = Counter(str(record["decision"]) for record in records)
    summary = {
        "resolver_key": RESOLVER_KEY,
        "source_policy": SOURCE_POLICY,
        "reuse_policy": REUSE_POLICY,
        "total_reviewed": len(records),
        "suggestion_candidates": int(decisions.get("suggestion_candidate_holy_site_concept_preserved", 0)),
        "guarded_no_apply": int(decisions.get("guarded_no_apply_reuse_effect_list_concept", 0)),
        "blocked_by_domain_ambiguity": int(decisions.get("blocked_by_domain_ambiguity", 0)),
        "blocked_by_context": int(decisions.get("blocked_by_context", 0)),
        "blocked_by_token_integrity": int(decisions.get("blocked_by_token_integrity", 0)),
        "blocked_no_safe_change": int(decisions.get("blocked_no_safe_change", 0)),
        "would_change_output": sum(1 for record in records if record["would_change_output"]),
        "confirmed_matches_output": sum(1 for record in records if record["guards"].get("confirmed_matches_output")),
        "false_safe_risk_count": sum(1 for record in records if record["false_safe_risk"]),
        "requires_apply_later_count": sum(1 for record in records if record["requires_apply_later"]),
        "requires_lifecycle_later_count": sum(1 for record in records if record["requires_lifecycle_later"]),
        "holy_site_concept_guard_ok_count": sum(1 for record in records if record["guards"].get("holy_site_concept_guard_ok")),
        "holy_site_concept_guard_failed_count": sum(1 for record in records if not record["guards"].get("holy_site_concept_guard_ok")),
        "reuse_policy_guard_ok_count": sum(1 for record in records if record["guards"].get("reuse_policy_guard_ok")),
        "token_integrity_ok_count": sum(1 for record in records if record["guards"].get("token_integrity_ok")),
        "sample_ids": [int(record["segment_id"]) for record in records],
        "decision_counts": dict(sorted(decisions.items())),
        "next_prompt": "chat_exec_effect_list_concept_resolver_dry_run_prompt.md",
    }
    if summary["total_reviewed"] != EXPECTED_TOTAL:
        raise SystemExit(f"summary total guard failed: {summary['total_reviewed']}")
    if summary["holy_site_concept_guard_failed_count"] != 0:
        summary["next_prompt"] = "chat_exec_holy_site_effect_name_domain_audit_prompt.md"
    if summary["requires_lifecycle_later_count"] != 0:
        summary["next_prompt"] = "chat_exec_holy_site_effect_name_domain_audit_prompt.md"
    if summary["false_safe_risk_count"] != 0:
        summary["next_prompt"] = "chat_exec_holy_site_effect_name_domain_audit_prompt.md"
    return summary


def write_outputs(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    txt_path, jsonl_path, summary_path = output_paths()
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "holy_site effect-name resolver dry-run",
        f"resolver_key={RESOLVER_KEY}",
        f"source_policy={SOURCE_POLICY}",
        f"reuse_policy={REUSE_POLICY}",
        "",
        f"total_reviewed={summary['total_reviewed']}",
        f"suggestion_candidates={summary['suggestion_candidates']}",
        f"guarded_no_apply={summary['guarded_no_apply']}",
        f"blocked_by_domain_ambiguity={summary['blocked_by_domain_ambiguity']}",
        f"blocked_by_context={summary['blocked_by_context']}",
        f"blocked_by_token_integrity={summary['blocked_by_token_integrity']}",
        f"blocked_no_safe_change={summary['blocked_no_safe_change']}",
        f"would_change_output={summary['would_change_output']}",
        f"confirmed_matches_output={summary['confirmed_matches_output']}",
        f"false_safe_risk_count={summary['false_safe_risk_count']}",
        f"requires_apply_later_count={summary['requires_apply_later_count']}",
        f"requires_lifecycle_later_count={summary['requires_lifecycle_later_count']}",
        f"holy_site_concept_guard_ok_count={summary['holy_site_concept_guard_ok_count']}",
        f"holy_site_concept_guard_failed_count={summary['holy_site_concept_guard_failed_count']}",
        f"reuse_policy_guard_ok_count={summary['reuse_policy_guard_ok_count']}",
        f"token_integrity_ok_count={summary['token_integrity_ok_count']}",
        "",
        "answers:",
        f"1. Segmentos revisados: {summary['total_reviewed']}.",
        f"2. suggestion_candidate: {summary['suggestion_candidates']}.",
        f"3. guarded_no_apply: {summary['guarded_no_apply']}.",
        f"4. Guard de holy-site/concept falhou: {'sim' if summary['holy_site_concept_guard_failed_count'] else 'nao'}.",
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
    parser = argparse.ArgumentParser(description="Dry-run resolver for holy-site effect-name concept reuse.")
    parser.add_argument("--holy-site-jsonl", required=True, type=Path)
    parser.add_argument("--holy-site-spec-json", required=True, type=Path)
    parser.add_argument("--registry-jsonl", required=True, type=Path)
    parser.add_argument("--strategy-plan-json", required=True, type=Path)
    parser.add_argument("--previous-resolver-summary-json", required=True, type=Path)
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    args = parser.parse_args()
    if args.segment_state_run_id != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit(f"segment_state_run_id guard failed: {args.segment_state_run_id}")
    review_rows = read_jsonl(args.holy_site_jsonl)
    review_samples = sample_rows(review_rows)
    validate_inputs(
        spec=read_json(args.holy_site_spec_json),
        review_samples=review_samples,
        registry_summary=summary_row(read_jsonl(args.registry_jsonl), "holy-site registry"),
        strategy=read_json(args.strategy_plan_json),
        previous_summary=read_json(args.previous_resolver_summary_json),
    )
    segment_ids = [int(row["segment_id"]) for row in review_samples]
    with connect_readonly() as conn:
        text_by_id = fetch_texts(conn, segment_ids, args.segment_state_run_id)
    if set(text_by_id) != set(segment_ids):
        raise SystemExit("segment_state lookup guard failed")
    records = [make_record(row, text_by_id[int(row["segment_id"])]) for row in review_samples]
    summary = build_summary(records)
    txt_path, jsonl_path, summary_path = write_outputs(records, summary)
    print(f"txt: {txt_path}")
    print(f"jsonl: {jsonl_path}")
    print(f"summary: {summary_path}")
    for key in [
        "total_reviewed",
        "suggestion_candidates",
        "guarded_no_apply",
        "blocked_by_domain_ambiguity",
        "blocked_by_token_integrity",
        "holy_site_concept_guard_failed_count",
        "false_safe_risk_count",
        "requires_lifecycle_later_count",
    ]:
        print(f"{key}: {summary[key]}")
    print(f"next_prompt: {summary['next_prompt']}")


if __name__ == "__main__":
    main()
