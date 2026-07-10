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


RESOLVER_KEY = "effect_list_trait_accolade_resolver_dry_run"
SOURCE_POLICY = "effect_list_trait_accolade_policy"
EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76
EXPECTED_TOTAL = 46
REUSE_POLICIES = {
    "get_trait_accolade_requirement_policy",
    "acclaimed_knight_entity_unlock_final_policy",
    "accolade_knight_attribute_policy",
    "knight_attribute_unlock_requirement_policy",
}

TRAIT_ACCOLADE_RE = re.compile(
    r"GetTrait|trait|Trait|Accolade|accolade|acclaimed_knight|knight|Knight|glory|accolade_[A-Za-z0-9_]+"
)
BRACKET_RE = re.compile(r"\[[^\]]+\]")
VARIABLE_RE = re.compile(r"\$[^$]+\$")
FORMATTING_TAG_RE = re.compile(r"#[A-Za-z][A-Za-z0-9_:.{};,|]*|#!|#P|#N|#D|#I")
DYNAMIC_RE = re.compile(
    r"Custom\(|Select_CString|GetTrait|GetName|GetGlory|GetAvailableLevelUpPoints|GetGloryForNextUnlock|ROOT\.|FROM\.|SCOPE\.|TARGET\.|\[[^\]]+\]|\$[^$]+\$"
)
SCOPE_RE = re.compile(r"ROOT\.|FROM\.|SCOPE\.|TARGET\.|Get[A-Za-z0-9_]+")


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_effect_list_trait_accolade_resolver_dry_run"
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


def summary_row(rows: list[dict[str, Any]], label: str) -> dict[str, Any]:
    summaries = [row for row in rows if row.get("record_type") == "summary"]
    if len(summaries) != 1:
        raise SystemExit(f"expected exactly one summary in {label}, got {len(summaries)}")
    return summaries[0]


def sample_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    samples = [row for row in rows if row.get("record_type") == "sample_review"]
    ids = [int(row["segment_id"]) for row in samples]
    if len(ids) != len(set(ids)):
        raise SystemExit("duplicate sample segment_id in effect-list trait review")
    return samples


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


def validate_inputs(
    spec: dict[str, Any],
    samples: list[dict[str, Any]],
    registry: dict[str, Any],
    previous: dict[str, Any],
    wave1: dict[str, Any],
) -> None:
    if spec.get("policy_id") != SOURCE_POLICY:
        raise SystemExit("spec policy_id guard failed")
    if int(spec.get("segment_state_run_id") or 0) != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("spec segment_state_run_id guard failed")
    if int(spec.get("ledger_run_id") or 0) != EXPECTED_LEDGER_RUN_ID:
        raise SystemExit("spec ledger_run_id guard failed")
    if int(spec.get("sampled") or 0) != EXPECTED_TOTAL:
        raise SystemExit("spec sampled guard failed")
    if int(spec.get("reuse_cataloged_policy_count") or 0) != EXPECTED_TOTAL:
        raise SystemExit("spec reuse guard failed")
    if len(samples) != EXPECTED_TOTAL:
        raise SystemExit(f"review_total guard failed: {len(samples)}")

    decisions = Counter(str(row.get("effect_trait_decision") or "") for row in samples)
    reuse_count = sum(count for decision, count in decisions.items() if decision.startswith("effect_trait_reuse_"))
    if reuse_count != EXPECTED_TOTAL:
        raise SystemExit(f"reuse count guard failed: {reuse_count}")
    for row in samples:
        if str(row.get("matched_registered_policy") or "") not in REUSE_POLICIES:
            raise SystemExit(f"bad reuse policy: {row.get('segment_id')}")
        if row.get("requires_apply_later") is True or row.get("requires_lifecycle_later") is True:
            raise SystemExit(f"future flag guard failed: {row.get('segment_id')}")

    if str(registry.get("mode") or "") != "apply":
        raise SystemExit("registry mode guard failed")
    if int(registry.get("review_total") or 0) != EXPECTED_TOTAL:
        raise SystemExit("registry review_total guard failed")
    if int(registry.get("reuse_cataloged_policies") or 0) != EXPECTED_TOTAL:
        raise SystemExit("registry reuse guard failed")
    if any(int(registry.get(key) or 0) for key in ["auto_apply_allowed", "lifecycle_allowed", "production_release_allowed"]):
        raise SystemExit("registry permission guard failed")

    if int(previous.get("total_reviewed") or 0) != 55 or int(previous.get("false_safe_risk_count") or 0) != 0:
        raise SystemExit("previous resolver guard failed")
    if int(wave1.get("wave1_false_safe_risk_count") or 0) != 0 or int(wave1.get("wave1_suggestion_candidates") or 0) != 0:
        raise SystemExit("wave1 summary guard failed")


def token_inventory(value: str) -> dict[str, list[str]]:
    return {
        "trait_accolade_tokens": TRAIT_ACCOLADE_RE.findall(value),
        "bracket_expressions": BRACKET_RE.findall(value),
        "variables": VARIABLE_RE.findall(value),
        "formatting_tags": FORMATTING_TAG_RE.findall(value),
        "dynamic_tokens": DYNAMIC_RE.findall(value),
        "scope_getters": SCOPE_RE.findall(value),
        "line_breaks": ["\\n"] if "\n" in value else [],
    }


def token_integrity_ok(value: str) -> bool:
    return value.count("[") == value.count("]") and value.count("$") % 2 == 0


def marker_list(row: dict[str, Any], key: str) -> list[str]:
    value = row.get(key) or []
    return value if isinstance(value, list) else []


def proposed_text(sample: dict[str, Any], current: str, original: str) -> str:
    corrected = str(sample.get("corrected_text") or "").strip()
    if corrected and corrected != current and corrected != original:
        return corrected
    return current or original


def make_record(sample: dict[str, Any], text: dict[str, Any]) -> dict[str, Any]:
    original = str(text.get("old_text") or sample.get("old_text") or "")
    current = str(text.get("output_text") or sample.get("output_text") or "")
    proposed = proposed_text(sample, current, original)
    reuse_policy = str(sample.get("matched_registered_policy") or "")
    decision_key = str(sample.get("effect_trait_decision") or "")
    original_tokens = token_inventory(original)
    proposed_tokens = token_inventory(proposed)
    secondary_markers = marker_list(sample, "secondary_markers")
    trait_markers = marker_list(sample, "trait_markers")
    accolade_markers = marker_list(sample, "accolade_markers")
    knight_markers = marker_list(sample, "knight_markers")
    would_change = proposed != current
    marker_evidence = bool(
        trait_markers
        or accolade_markers
        or knight_markers
        or "TraitAccolade" in secondary_markers
        or "AccoladeTrait" in secondary_markers
        or "KnightAttribute" in secondary_markers
    )

    guards = {
        "state_pending": str(text.get("state_group") or "") == "pending" and int(text.get("is_closed") or 0) == 0,
        "needs_output_apply_zero": int(text.get("needs_output_apply") or 0) == 0,
        "confirmed_matches_output": int(text.get("confirmed_matches_output") or 0) == 1,
        "marker_evidence_present": marker_evidence,
        "reuse_policy_guard_ok": reuse_policy in REUSE_POLICIES,
        "trait_accolade_tokens_preserved": original_tokens["trait_accolade_tokens"] == proposed_tokens["trait_accolade_tokens"],
        "bracket_expressions_preserved": original_tokens["bracket_expressions"] == proposed_tokens["bracket_expressions"],
        "variables_preserved": original_tokens["variables"] == proposed_tokens["variables"],
        "formatting_tags_preserved": original_tokens["formatting_tags"] == proposed_tokens["formatting_tags"],
        "dynamic_tokens_preserved": original_tokens["dynamic_tokens"] == proposed_tokens["dynamic_tokens"],
        "scope_getters_preserved": original_tokens["scope_getters"] == proposed_tokens["scope_getters"],
        "line_breaks_preserved": original.count("\n") == proposed.count("\n"),
        "token_integrity_ok": token_integrity_ok(original) and token_integrity_ok(proposed),
    }
    guards["trait_accolade_guard_ok"] = (
        guards["marker_evidence_present"]
        and guards["reuse_policy_guard_ok"]
        and guards["trait_accolade_tokens_preserved"]
    )

    if not guards["state_pending"] or not guards["needs_output_apply_zero"] or not guards["confirmed_matches_output"]:
        decision, notes, risk = "blocked_by_context", "state guard failed in selected run", True
    elif not guards["token_integrity_ok"] or not guards["bracket_expressions_preserved"] or not guards["variables_preserved"] or not guards["formatting_tags_preserved"]:
        decision, notes, risk = "blocked_by_token_integrity", "CK3 token/bracket/variable/tag guard failed", True
    elif not guards["trait_accolade_tokens_preserved"]:
        decision, notes, risk = "blocked_by_trait_accolade_integrity", "trait/accolade/knight token surface would change", True
    elif not guards["trait_accolade_guard_ok"]:
        decision, notes, risk = "blocked_by_domain_ambiguity", "trait/accolade/knight evidence incomplete", True
    elif would_change:
        decision, notes, risk = "suggestion_candidate_trait_accolade_preserved", "explicit corrected_text differs and all trait/accolade guards pass; audit required before any apply", False
    else:
        decision, notes, risk = "guarded_no_apply_reuse_policy", "cataloged reuse policy guards this trait/accolade effect-list segment; no safe textual change is proposed", False

    return {
        "segment_id": int(sample["segment_id"]),
        "resolver_key": RESOLVER_KEY,
        "source_policy": SOURCE_POLICY,
        "reuse_policy": reuse_policy,
        "effect_trait_decision": decision_key,
        "original_text": original,
        "current_output_text": current,
        "proposed_text": proposed,
        "decision": decision,
        "guards": guards,
        "would_change_output": bool(decision == "suggestion_candidate_trait_accolade_preserved" and would_change),
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
        "suggestion_candidates": decisions.get("suggestion_candidate_trait_accolade_preserved", 0),
        "guarded_no_apply": decisions.get("guarded_no_apply_reuse_policy", 0),
        "guarded_no_apply_reuse_policy": decisions.get("guarded_no_apply_reuse_policy", 0),
        "blocked_by_trait_accolade_integrity": decisions.get("blocked_by_trait_accolade_integrity", 0),
        "blocked_by_domain_ambiguity": decisions.get("blocked_by_domain_ambiguity", 0),
        "blocked_by_context": decisions.get("blocked_by_context", 0),
        "blocked_by_token_integrity": decisions.get("blocked_by_token_integrity", 0),
        "blocked_no_safe_change": decisions.get("blocked_no_safe_change", 0),
        "would_change_output": sum(1 for record in records if record["would_change_output"]),
        "confirmed_matches_output": sum(1 for record in records if record["guards"].get("confirmed_matches_output")),
        "false_safe_risk_count": sum(1 for record in records if record["false_safe_risk"]),
        "requires_apply_later_count": sum(1 for record in records if record["requires_apply_later"]),
        "requires_lifecycle_later_count": sum(1 for record in records if record["requires_lifecycle_later"]),
        "trait_accolade_guard_ok_count": sum(1 for record in records if record["guards"].get("trait_accolade_guard_ok")),
        "trait_accolade_guard_failed_count": sum(1 for record in records if not record["guards"].get("trait_accolade_guard_ok")),
        "reuse_policy_guard_ok_count": sum(1 for record in records if record["guards"].get("reuse_policy_guard_ok")),
        "token_integrity_ok_count": sum(1 for record in records if record["guards"].get("token_integrity_ok")),
        "sample_ids": [int(record["segment_id"]) for record in records],
        "decision_counts": dict(sorted(decisions.items())),
        "next_prompt": "chat_exec_resolver_wave2_consolidated_diagnostic_prompt.md",
    }
    if summary["suggestion_candidates"]:
        summary["next_prompt"] = "chat_exec_effect_list_trait_accolade_candidate_audit_prompt.md"
    if (
        summary["false_safe_risk_count"]
        or summary["trait_accolade_guard_failed_count"]
        or summary["blocked_by_token_integrity"]
    ):
        summary["next_prompt"] = "chat_exec_effect_list_trait_accolade_integrity_audit_prompt.md"
    return summary


def write_outputs(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    txt_path, jsonl_path, summary_path = output_paths()
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    metric_keys = [
        "total_reviewed",
        "suggestion_candidates",
        "guarded_no_apply",
        "guarded_no_apply_reuse_policy",
        "blocked_by_trait_accolade_integrity",
        "blocked_by_domain_ambiguity",
        "blocked_by_context",
        "blocked_by_token_integrity",
        "blocked_no_safe_change",
        "would_change_output",
        "confirmed_matches_output",
        "false_safe_risk_count",
        "requires_apply_later_count",
        "requires_lifecycle_later_count",
        "trait_accolade_guard_ok_count",
        "trait_accolade_guard_failed_count",
        "reuse_policy_guard_ok_count",
        "token_integrity_ok_count",
    ]
    lines = [
        "effect-list trait/accolade resolver dry-run",
        f"resolver_key={RESOLVER_KEY}",
        f"source_policy={SOURCE_POLICY}",
        "",
        *[f"{key}={summary[key]}" for key in metric_keys],
        "",
        "answers:",
        f"1. Segmentos revisados: {summary['total_reviewed']}.",
        f"2. suggestion_candidate: {summary['suggestion_candidates']}.",
        f"3. guarded_no_apply: {summary['guarded_no_apply']}.",
        f"4. Guard de trait/accolade/knight falhou: {'sim' if summary['trait_accolade_guard_failed_count'] else 'nao'}.",
        f"5. Exige lifecycle/apply: lifecycle={summary['requires_lifecycle_later_count']}, apply={summary['requires_apply_later_count']}.",
        f"6. Risco false-safe: {summary['false_safe_risk_count']}.",
        "7. Apply futuro: ainda nao; este dry-run validou reuso/guard, mas nao gerou proposta de mudanca.",
        f"8. Proximo prompt recomendado: {summary['next_prompt']}.",
        "",
        "production_full_recommended=false",
        "network_update_now=false",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--effect-trait-jsonl", required=True, type=Path)
    parser.add_argument("--effect-trait-spec-json", required=True, type=Path)
    parser.add_argument("--registry-jsonl", required=True, type=Path)
    parser.add_argument("--previous-resolver-summary-json", required=True, type=Path)
    parser.add_argument("--wave1-summary-json", required=True, type=Path)
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    args = parser.parse_args()
    if args.segment_state_run_id != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("segment_state_run_id argument guard failed")

    review_rows = read_jsonl(args.effect_trait_jsonl)
    samples = sample_rows(review_rows)
    spec = read_json(args.effect_trait_spec_json)
    registry = summary_row(read_jsonl(args.registry_jsonl), "registry apply")
    previous = read_json(args.previous_resolver_summary_json)
    wave1 = read_json(args.wave1_summary_json)
    validate_inputs(spec, samples, registry, previous, wave1)

    with connect_readonly() as conn:
        texts = fetch_texts(conn, [int(row["segment_id"]) for row in samples], args.segment_state_run_id)
    missing = sorted(set(int(row["segment_id"]) for row in samples) - set(texts))
    if missing:
        raise SystemExit(f"missing segment_state rows: {missing[:10]}")

    records = [make_record(row, texts[int(row["segment_id"])]) for row in samples]
    summary = build_summary(records)
    if summary["total_reviewed"] != EXPECTED_TOTAL:
        raise SystemExit("total_reviewed output guard failed")
    if summary["trait_accolade_guard_failed_count"] or summary["false_safe_risk_count"] or summary["requires_lifecycle_later_count"] or summary["blocked_by_token_integrity"]:
        summary["next_prompt"] = "chat_exec_effect_list_trait_accolade_integrity_audit_prompt.md"
    txt_path, jsonl_path, summary_path = write_outputs(records, summary)
    print(f"txt: {txt_path}")
    print(f"jsonl: {jsonl_path}")
    print(f"summary: {summary_path}")
    for key in [
        "total_reviewed",
        "suggestion_candidates",
        "guarded_no_apply",
        "blocked_by_trait_accolade_integrity",
        "blocked_by_domain_ambiguity",
        "blocked_by_token_integrity",
        "trait_accolade_guard_failed_count",
        "false_safe_risk_count",
        "requires_lifecycle_later_count",
        "next_prompt",
    ]:
        print(f"{key}: {summary[key]}")


if __name__ == "__main__":
    main()
