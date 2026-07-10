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


RESOLVER_KEY = "effect_list_gender_local_player_resolver_dry_run"
SOURCE_POLICY = "effect_list_gender_local_player_policy"
EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76
EXPECTED_TOTAL = 55
EXPECTED_REUSE_TOTAL = 52
EXPECTED_RESIDUAL_TOTAL = 3
REUSE_POLICIES = {
    "local_player_requirement_policy",
    "select_cstring_es_helper_policy",
    "select_cstring_player_target_direct_policy",
    "select_cstring_possessive_policy",
}

SELECT_CSTRING_RE = re.compile(r"Select_CString\([^]]+\)")
ES_HELPER_RE = re.compile(r"\.Custom\('ES_[A-Za-z0-9_]+'\)")
BRACKET_RE = re.compile(r"\[[^\]]+\]")
VARIABLE_RE = re.compile(r"\$[^$]+\$")
FORMATTING_TAG_RE = re.compile(r"#[A-Za-z][A-Za-z0-9_:.{};,|]*|#!|#P|#N|#D")
DYNAMIC_RE = re.compile(
    r"Custom\(|Select_CString|GetPlayer|LocalPlayer|GetHerHim|GetSheHe|GetHisHer|GetWomanMan|ROOT\.|FROM\.|SCOPE\.|TARGET\.|\[[^\]]+\]|\$[^$]+\$"
)
SCOPE_RE = re.compile(r"ROOT\.|FROM\.|SCOPE\.|TARGET\.|Get[A-Za-z0-9_]+")


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_effect_list_gender_local_player_resolver_dry_run"
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
        raise SystemExit("duplicate sample segment_id in effect-list gender review")
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
    wave1: dict[str, Any],
    strategy: dict[str, Any],
) -> None:
    if spec.get("policy_id") != SOURCE_POLICY:
        raise SystemExit("spec policy_id guard failed")
    if int(spec.get("segment_state_run_id") or 0) != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("spec segment_state_run_id guard failed")
    if int(spec.get("ledger_run_id") or 0) != EXPECTED_LEDGER_RUN_ID:
        raise SystemExit("spec ledger_run_id guard failed")
    if int(spec.get("sampled") or 0) != EXPECTED_TOTAL:
        raise SystemExit("spec sampled guard failed")
    if int(spec.get("reuse_registered_policy_count") or 0) != EXPECTED_REUSE_TOTAL:
        raise SystemExit("spec reuse guard failed")
    if len(samples) != EXPECTED_TOTAL:
        raise SystemExit(f"review_total guard failed: {len(samples)}")

    decisions = Counter(str(row.get("effect_gender_decision") or "") for row in samples)
    reuse_count = sum(count for decision, count in decisions.items() if decision.startswith("effect_gender_reuse_"))
    if reuse_count != EXPECTED_REUSE_TOTAL:
        raise SystemExit(f"reuse count guard failed: {reuse_count}")
    if decisions.get("needs_effect_gender_residual_repair", 0) != EXPECTED_RESIDUAL_TOTAL:
        raise SystemExit("residual count guard failed")
    for row in samples:
        decision = str(row.get("effect_gender_decision") or "")
        reuse_policy = str(row.get("matched_registered_policy") or "")
        if decision.startswith("effect_gender_reuse_") and reuse_policy not in REUSE_POLICIES:
            raise SystemExit(f"bad reuse policy: {row.get('segment_id')}")
        if row.get("requires_apply_later") is True or row.get("requires_lifecycle_later") is True:
            raise SystemExit(f"future flag guard failed: {row.get('segment_id')}")

    if str(registry.get("mode") or "") != "apply":
        raise SystemExit("registry mode guard failed")
    if int(registry.get("review_total") or 0) != EXPECTED_TOTAL:
        raise SystemExit("registry review_total guard failed")
    if int(registry.get("reuse_registered_policies") or 0) != EXPECTED_REUSE_TOTAL:
        raise SystemExit("registry reuse guard failed")
    if any(int(registry.get(key) or 0) for key in ["auto_apply_allowed", "lifecycle_allowed", "production_release_allowed"]):
        raise SystemExit("registry permission guard failed")

    if int(wave1.get("wave1_suggestion_candidates") or 0) != 0:
        raise SystemExit("wave1 suggestion guard failed")
    if int(wave1.get("wave1_false_safe_risk_count") or 0) != 0:
        raise SystemExit("wave1 false-safe guard failed")
    if int(strategy.get("segment_state_run_id") or 0) != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("strategy segment_state_run_id guard failed")
    candidate = next((row for row in strategy.get("candidate_ranking", []) if row.get("policy_key") == SOURCE_POLICY), None)
    if not candidate or int(candidate.get("candidate_segment_count") or 0) != EXPECTED_TOTAL:
        raise SystemExit("strategy candidate guard failed")


def token_inventory(value: str) -> dict[str, list[str]]:
    return {
        "select_cstring": SELECT_CSTRING_RE.findall(value),
        "es_helpers": ES_HELPER_RE.findall(value),
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
    decision_key = str(sample.get("effect_gender_decision") or "")
    reuse_policy = str(sample.get("matched_registered_policy") or "")
    original_tokens = token_inventory(original)
    proposed_tokens = token_inventory(proposed)
    gender_markers = marker_list(sample, "gender_markers")
    local_player_markers = marker_list(sample, "local_player_markers")
    select_markers = marker_list(sample, "select_cstring_markers")
    es_markers = marker_list(sample, "es_helper_markers")
    secondary_markers = marker_list(sample, "secondary_markers")

    has_select = bool(select_markers or original_tokens["select_cstring"])
    has_es = bool(es_markers or original_tokens["es_helpers"])
    has_possessive = "Possessive" in secondary_markers
    is_residual = decision_key == "needs_effect_gender_residual_repair"
    is_reuse = decision_key.startswith("effect_gender_reuse_")
    would_change = proposed != current

    guards = {
        "state_pending": str(text.get("state_group") or "") == "pending" and int(text.get("is_closed") or 0) == 0,
        "needs_output_apply_zero": int(text.get("needs_output_apply") or 0) == 0,
        "confirmed_matches_output": int(text.get("confirmed_matches_output") or 0) == 1,
        "perspective_marker_present": bool(gender_markers or local_player_markers or "GenderLocalPlayer" in secondary_markers),
        "local_player_marker_present": bool(local_player_markers or "LocalPlayer" in secondary_markers),
        "select_cstring_present": has_select,
        "es_helper_present": has_es,
        "possessive_present": has_possessive,
        "reuse_policy_guard_ok": reuse_policy in REUSE_POLICIES,
        "select_cstring_byte_equivalent": original_tokens["select_cstring"] == proposed_tokens["select_cstring"],
        "es_helper_byte_equivalent": original_tokens["es_helpers"] == proposed_tokens["es_helpers"],
        "bracket_expressions_preserved": original_tokens["bracket_expressions"] == proposed_tokens["bracket_expressions"],
        "variables_preserved": original_tokens["variables"] == proposed_tokens["variables"],
        "formatting_tags_preserved": original_tokens["formatting_tags"] == proposed_tokens["formatting_tags"],
        "dynamic_tokens_preserved": original_tokens["dynamic_tokens"] == proposed_tokens["dynamic_tokens"],
        "scope_getters_preserved": original_tokens["scope_getters"] == proposed_tokens["scope_getters"],
        "line_breaks_preserved": original.count("\n") == proposed.count("\n"),
        "token_integrity_ok": token_integrity_ok(original) and token_integrity_ok(proposed),
    }
    guards["select_cstring_guard_ok"] = (not has_select) or guards["select_cstring_byte_equivalent"]
    guards["es_helper_guard_ok"] = (not has_es) or guards["es_helper_byte_equivalent"]
    guards["possessive_guard_ok"] = (not has_possessive) or is_reuse or is_residual
    guards["perspective_guard_ok"] = guards["perspective_marker_present"] and (is_reuse or is_residual)
    guards["local_player_guard_ok"] = guards["local_player_marker_present"] or is_reuse or is_residual

    if not guards["state_pending"] or not guards["needs_output_apply_zero"] or not guards["confirmed_matches_output"]:
        decision, notes, risk = "blocked_by_context", "state guard failed in selected run", True
    elif not guards["token_integrity_ok"] or not guards["bracket_expressions_preserved"] or not guards["variables_preserved"] or not guards["formatting_tags_preserved"]:
        decision, notes, risk = "blocked_by_token_integrity", "CK3 token/bracket/variable/tag guard failed", True
    elif not guards["select_cstring_guard_ok"]:
        decision, notes, risk = "blocked_by_select_cstring_integrity", "Select_CString structure would change", True
    elif not guards["es_helper_guard_ok"]:
        decision, notes, risk = "blocked_by_es_helper_integrity", "ES helper structure would change", True
    elif not guards["possessive_guard_ok"]:
        decision, notes, risk = "blocked_by_possessive_ambiguity", "possessive perspective is not safe", True
    elif not guards["perspective_guard_ok"] or not guards["local_player_guard_ok"]:
        decision, notes, risk = "blocked_by_perspective_ambiguity", "player/target/local-player perspective is not unequivocal", True
    elif is_residual:
        decision, notes, risk = "guarded_no_apply_residual_preserved", "visible residual is preserved for a later resolver; no safe textual change is proposed", False
    elif would_change and is_reuse and guards["reuse_policy_guard_ok"]:
        decision, notes, risk = "suggestion_candidate_gender_local_player_preserved", "explicit corrected_text differs and all perspective/token guards pass; audit required before any apply", False
    elif is_reuse and guards["reuse_policy_guard_ok"]:
        decision, notes, risk = "guarded_no_apply_reuse_policy", "registered reuse policy guards this gender/local-player effect-list segment; no safe textual change is proposed", False
    else:
        decision, notes, risk = "blocked_no_safe_change", "no safe resolver action available", False

    return {
        "segment_id": int(sample["segment_id"]),
        "resolver_key": RESOLVER_KEY,
        "source_policy": SOURCE_POLICY,
        "reuse_policy": reuse_policy,
        "effect_gender_decision": decision_key,
        "original_text": original,
        "current_output_text": current,
        "proposed_text": proposed,
        "decision": decision,
        "guards": guards,
        "would_change_output": bool(decision == "suggestion_candidate_gender_local_player_preserved" and would_change),
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
        "suggestion_candidates": decisions.get("suggestion_candidate_gender_local_player_preserved", 0),
        "guarded_no_apply": decisions.get("guarded_no_apply_reuse_policy", 0) + decisions.get("guarded_no_apply_residual_preserved", 0),
        "guarded_no_apply_reuse_policy": decisions.get("guarded_no_apply_reuse_policy", 0),
        "guarded_no_apply_residual_preserved": decisions.get("guarded_no_apply_residual_preserved", 0),
        "blocked_by_perspective_ambiguity": decisions.get("blocked_by_perspective_ambiguity", 0),
        "blocked_by_select_cstring_integrity": decisions.get("blocked_by_select_cstring_integrity", 0),
        "blocked_by_possessive_ambiguity": decisions.get("blocked_by_possessive_ambiguity", 0),
        "blocked_by_es_helper_integrity": decisions.get("blocked_by_es_helper_integrity", 0),
        "blocked_by_token_integrity": decisions.get("blocked_by_token_integrity", 0),
        "blocked_by_context": decisions.get("blocked_by_context", 0),
        "blocked_no_safe_change": decisions.get("blocked_no_safe_change", 0),
        "would_change_output": sum(1 for record in records if record["would_change_output"]),
        "confirmed_matches_output": sum(1 for record in records if record["guards"].get("confirmed_matches_output")),
        "false_safe_risk_count": sum(1 for record in records if record["false_safe_risk"]),
        "requires_apply_later_count": sum(1 for record in records if record["requires_apply_later"]),
        "requires_lifecycle_later_count": sum(1 for record in records if record["requires_lifecycle_later"]),
        "perspective_guard_ok_count": sum(1 for record in records if record["guards"].get("perspective_guard_ok")),
        "perspective_guard_failed_count": sum(1 for record in records if not record["guards"].get("perspective_guard_ok")),
        "local_player_guard_ok_count": sum(1 for record in records if record["guards"].get("local_player_guard_ok")),
        "select_cstring_guard_ok_count": sum(1 for record in records if record["guards"].get("select_cstring_guard_ok")),
        "possessive_guard_ok_count": sum(1 for record in records if record["guards"].get("possessive_guard_ok")),
        "es_helper_guard_ok_count": sum(1 for record in records if record["guards"].get("es_helper_guard_ok")),
        "token_integrity_ok_count": sum(1 for record in records if record["guards"].get("token_integrity_ok")),
        "sample_ids": [int(record["segment_id"]) for record in records],
        "decision_counts": dict(sorted(decisions.items())),
        "next_prompt": "chat_exec_effect_list_trait_accolade_resolver_dry_run_prompt.md",
    }
    if summary["suggestion_candidates"]:
        summary["next_prompt"] = "chat_exec_effect_list_gender_local_player_candidate_audit_prompt.md"
    if summary["false_safe_risk_count"] or summary["blocked_by_token_integrity"] or summary["perspective_guard_failed_count"]:
        summary["next_prompt"] = "chat_exec_effect_list_gender_local_player_perspective_audit_prompt.md"
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
        "guarded_no_apply_residual_preserved",
        "blocked_by_perspective_ambiguity",
        "blocked_by_select_cstring_integrity",
        "blocked_by_possessive_ambiguity",
        "blocked_by_es_helper_integrity",
        "blocked_by_token_integrity",
        "blocked_by_context",
        "blocked_no_safe_change",
        "would_change_output",
        "confirmed_matches_output",
        "false_safe_risk_count",
        "requires_apply_later_count",
        "requires_lifecycle_later_count",
        "perspective_guard_ok_count",
        "perspective_guard_failed_count",
        "local_player_guard_ok_count",
        "select_cstring_guard_ok_count",
        "possessive_guard_ok_count",
        "es_helper_guard_ok_count",
        "token_integrity_ok_count",
    ]
    lines = [
        "effect-list gender/local-player resolver dry-run",
        f"resolver_key={RESOLVER_KEY}",
        f"source_policy={SOURCE_POLICY}",
        "",
        *[f"{key}={summary[key]}" for key in metric_keys],
        "",
        "answers:",
        f"1. Segmentos revisados: {summary['total_reviewed']}.",
        f"2. suggestion_candidate: {summary['suggestion_candidates']}.",
        f"3. guarded_no_apply: {summary['guarded_no_apply']}.",
        f"4. Bloqueados por perspectiva/local-player: {summary['blocked_by_perspective_ambiguity']}.",
        f"5. Falhas Select_CString/possessivo/ES helper: select={summary['blocked_by_select_cstring_integrity']}, possessive={summary['blocked_by_possessive_ambiguity']}, es={summary['blocked_by_es_helper_integrity']}.",
        f"6. Exige lifecycle/apply: lifecycle={summary['requires_lifecycle_later_count']}, apply={summary['requires_apply_later_count']}.",
        f"7. Risco false-safe: {summary['false_safe_risk_count']}.",
        "8. Apply futuro: ainda nao; candidatos exigem auditoria focal e nao houve proposta segura nesta run.",
        f"9. Proximo prompt recomendado: {summary['next_prompt']}.",
        "",
        "production_full_recommended=false",
        "network_update_now=false",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--effect-gender-jsonl", required=True, type=Path)
    parser.add_argument("--effect-gender-spec-json", required=True, type=Path)
    parser.add_argument("--registry-jsonl", required=True, type=Path)
    parser.add_argument("--wave1-summary-json", required=True, type=Path)
    parser.add_argument("--strategy-plan-json", required=True, type=Path)
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    args = parser.parse_args()
    if args.segment_state_run_id != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("segment_state_run_id argument guard failed")

    review_rows = read_jsonl(args.effect_gender_jsonl)
    samples = sample_rows(review_rows)
    spec = read_json(args.effect_gender_spec_json)
    registry = summary_row(read_jsonl(args.registry_jsonl), "registry apply")
    wave1 = read_json(args.wave1_summary_json)
    strategy = read_json(args.strategy_plan_json)
    validate_inputs(spec, samples, registry, wave1, strategy)

    with connect_readonly() as conn:
        texts = fetch_texts(conn, [int(row["segment_id"]) for row in samples], args.segment_state_run_id)
    missing = sorted(set(int(row["segment_id"]) for row in samples) - set(texts))
    if missing:
        raise SystemExit(f"missing segment_state rows: {missing[:10]}")

    records = [make_record(row, texts[int(row["segment_id"])]) for row in samples]
    summary = build_summary(records)
    if summary["total_reviewed"] != EXPECTED_TOTAL:
        raise SystemExit("total_reviewed output guard failed")
    if summary["false_safe_risk_count"] or summary["requires_lifecycle_later_count"] or summary["blocked_by_token_integrity"]:
        summary["next_prompt"] = "chat_exec_effect_list_gender_local_player_perspective_audit_prompt.md"
    txt_path, jsonl_path, summary_path = write_outputs(records, summary)
    print(f"txt: {txt_path}")
    print(f"jsonl: {jsonl_path}")
    print(f"summary: {summary_path}")
    for key in [
        "total_reviewed",
        "suggestion_candidates",
        "guarded_no_apply",
        "guarded_no_apply_reuse_policy",
        "guarded_no_apply_residual_preserved",
        "blocked_by_perspective_ambiguity",
        "blocked_by_select_cstring_integrity",
        "blocked_by_possessive_ambiguity",
        "blocked_by_es_helper_integrity",
        "blocked_by_token_integrity",
        "false_safe_risk_count",
        "requires_lifecycle_later_count",
        "next_prompt",
    ]:
        print(f"{key}: {summary[key]}")


if __name__ == "__main__":
    main()
