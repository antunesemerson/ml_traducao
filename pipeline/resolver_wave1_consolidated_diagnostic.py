from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import db


EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76

INPUTS = {
    "blocked_debug_marker": "reports/20260623_191436_181306_blocked_uncertain_token_integrity_debug_marker_resolver_dry_run_summary.json",
    "landed_title_dynasty_house": "reports/20260623_192123_336762_domain_context_landed_title_dynasty_house_resolver_dry_run_summary.json",
    "holy_site_effect_name": "reports/20260623_192609_207564_holy_site_effect_name_resolver_dry_run_summary.json",
    "effect_list_concept": "reports/20260623_193150_577980_effect_list_concept_resolver_dry_run_summary.json",
    "script_value_effect": "reports/20260623_194541_896909_script_value_effect_resolver_dry_run_summary.json",
    "numeric_modifier_audit": "reports/20260623_195052_431214_script_value_effect_resolver_audit_summary.json",
    "numeric_modifier_micro_policy": "reports/20260623_200555_837976_script_value_effect_numeric_modifier_policy_review_spec.json",
    "strategy": "reports/20260623_165502_188127_resolver_dry_run_strategy_plan.json",
    "inventory": "reports/20260623_164747_499852_global_final_architecture_before_resolution_diagnostic_inventory.json",
}


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_resolver_wave1_consolidated_diagnostic"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), reports_dir / f"{base.name}_summary.json"


def load_inputs() -> dict[str, dict[str, Any]]:
    loaded: dict[str, dict[str, Any]] = {}
    for key, rel_path in INPUTS.items():
        path = db.project_path(rel_path)
        if not path.exists():
            raise SystemExit(f"missing required artifact: {path}")
        loaded[key] = read_json(path)
    return loaded


def require_int(row: dict[str, Any], key: str, expected: int, label: str) -> None:
    actual = int(row.get(key) or 0)
    if actual != expected:
        raise SystemExit(f"{label}.{key} expected {expected}, got {actual}")


def validate_inputs(data: dict[str, dict[str, Any]], segment_state_run_id: int, ledger_run_id: int) -> None:
    if segment_state_run_id != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("segment_state_run_id argument guard failed")
    if ledger_run_id != EXPECTED_LEDGER_RUN_ID:
        raise SystemExit("ledger_run_id argument guard failed")

    expected_resolvers = {
        "blocked_debug_marker": (54, 0, 54),
        "landed_title_dynasty_house": (61, 0, 61),
        "holy_site_effect_name": (240, 0, 240),
        "effect_list_concept": (17, 0, 17),
        "script_value_effect": (240, 0, 237),
    }
    for key, (reviewed, candidates, guarded) in expected_resolvers.items():
        row = data[key]
        require_int(row, "total_reviewed", reviewed, key)
        require_int(row, "suggestion_candidates", candidates, key)
        require_int(row, "guarded_no_apply", guarded, key)
        require_int(row, "false_safe_risk_count", 0, key)
        require_int(row, "requires_apply_later_count", 0, key)
        require_int(row, "requires_lifecycle_later_count", 0, key)

    require_int(data["script_value_effect"], "blocked_by_numeric_or_modifier_guard", 3, "script_value_effect")
    require_int(data["numeric_modifier_audit"], "total_audited", 3, "numeric_modifier_audit")
    require_int(data["numeric_modifier_audit"], "percent_modifier_requires_policy", 3, "numeric_modifier_audit")
    require_int(data["numeric_modifier_audit"], "false_safe_risk_count", 0, "numeric_modifier_audit")
    require_int(data["numeric_modifier_micro_policy"], "total_reviewed", 3, "numeric_modifier_micro_policy")
    require_int(data["numeric_modifier_micro_policy"], "terminal_guard_count", 3, "numeric_modifier_micro_policy")
    require_int(data["numeric_modifier_micro_policy"], "false_safe_risk_count", 0, "numeric_modifier_micro_policy")
    if data["numeric_modifier_micro_policy"].get("register_component_now") is not False:
        raise SystemExit("micro-policy register_component_now guard failed")

    strategy = data["strategy"]
    if int(strategy.get("segment_state_run_id") or 0) != segment_state_run_id:
        raise SystemExit("strategy segment_state_run_id guard failed")
    if int(strategy.get("ledger_run_id") or 0) != ledger_run_id:
        raise SystemExit("strategy ledger_run_id guard failed")
    coverage = strategy.get("coverage") or {}
    require_int(coverage, "routed_or_spec_coverage_projected", 11690, "strategy.coverage")
    require_int(coverage, "segments_without_useful_spec", 35, "strategy.coverage")

    inventory_summary = data["inventory"].get("summary") or {}
    require_int(inventory_summary, "registered_agents", 238, "inventory.summary")
    require_int(inventory_summary, "observed_agent_keys", 295, "inventory.summary")
    require_int(inventory_summary, "issue_network_agents", 74, "inventory.summary")


def resolver_record(label: str, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "record_type": "wave1_component",
        "component": label,
        "resolver_key": row.get("resolver_key", label),
        "source_policy": row.get("source_policy", ""),
        "reviewed": int(row.get("total_reviewed") or row.get("total_audited") or 0),
        "suggestion_candidates": int(row.get("suggestion_candidates") or 0),
        "guarded_no_apply": int(row.get("guarded_no_apply") or 0),
        "blocked_by_token_integrity": int(row.get("blocked_by_token_integrity") or 0),
        "blocked_by_domain_ambiguity": int(row.get("blocked_by_domain_ambiguity") or 0),
        "blocked_by_context": int(row.get("blocked_by_context") or 0),
        "blocked_by_numeric_or_modifier_guard": int(row.get("blocked_by_numeric_or_modifier_guard") or 0),
        "false_safe_risk_count": int(row.get("false_safe_risk_count") or 0),
        "requires_apply_later_count": int(row.get("requires_apply_later_count") or 0),
        "requires_lifecycle_later_count": int(row.get("requires_lifecycle_later_count") or 0),
        "would_change_output": int(row.get("would_change_output") or 0),
    }


def choose_next(data: dict[str, dict[str, Any]]) -> tuple[str, str]:
    ranking = data["strategy"].get("candidate_ranking") or []
    by_policy = {row.get("policy_key"): row for row in ranking}
    gender = by_policy.get("effect_list_gender_local_player_policy", {})
    trait = by_policy.get("effect_list_trait_accolade_policy", {})
    # Wave 1 was all zero-yield. Choose the active path with higher chance of candidates,
    # while keeping perspective guards explicit in the next prompt.
    if gender:
        return (
            "chat_exec_effect_list_gender_local_player_resolver_dry_run_prompt.md",
            "buscar candidatos reais depois de uma onda 1 totalmente guardada; o bloco tem yield esperado medio, mas deve usar guardas fortes de perspectiva/local-player",
        )
    if trait:
        return (
            "chat_exec_effect_list_trait_accolade_resolver_dry_run_prompt.md",
            "continuar em baixo risco com reuso catalogado completo de trait/accolade",
        )
    return ("chat_exec_effect_list_trait_accolade_resolver_dry_run_prompt.md", "fallback conservador")


def build_summary(data: dict[str, dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    component_keys = [
        "blocked_debug_marker",
        "landed_title_dynasty_house",
        "holy_site_effect_name",
        "effect_list_concept",
        "script_value_effect",
    ]
    records = [resolver_record(key, data[key]) for key in component_keys]
    audit = data["numeric_modifier_audit"]
    micro = data["numeric_modifier_micro_policy"]
    records.append(
        {
            "record_type": "wave1_audit",
            "component": "numeric_modifier_audit",
            "audit_key": audit.get("audit_key"),
            "reviewed": int(audit.get("total_audited") or 0),
            "percent_modifier_requires_policy": int(audit.get("percent_modifier_requires_policy") or 0),
            "false_safe_risk_count": int(audit.get("false_safe_risk_count") or 0),
            "requires_apply_later_count": int(audit.get("requires_apply_later_count") or 0),
            "requires_lifecycle_later_count": int(audit.get("requires_lifecycle_later_count") or 0),
        }
    )
    records.append(
        {
            "record_type": "wave1_micro_policy",
            "component": "numeric_modifier_micro_policy",
            "micro_policy": micro.get("micro_policy"),
            "reviewed": int(micro.get("total_reviewed") or 0),
            "terminal_guard_count": int(micro.get("terminal_guard_count") or 0),
            "register_component_now": bool(micro.get("register_component_now")),
            "false_safe_risk_count": int(micro.get("false_safe_risk_count") or 0),
            "requires_apply_later_count": int(micro.get("requires_apply_later_count") or 0),
            "requires_lifecycle_later_count": int(micro.get("requires_lifecycle_later_count") or 0),
        }
    )

    next_prompt, rationale = choose_next(data)
    resolver_records = records[:5]
    summary = {
        "schema_version": 1,
        "source": "resolver_wave1_consolidated_diagnostic_v1",
        "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
        "ledger_run_id": EXPECTED_LEDGER_RUN_ID,
        "coverage_before_resolvers": "11690/11725",
        "segments_without_useful_spec_before_resolvers": 35,
        "wave1_total_reviewed": sum(row["reviewed"] for row in resolver_records) + int(micro.get("total_reviewed") or 0),
        "wave1_resolver_total_reviewed": sum(row["reviewed"] for row in resolver_records),
        "wave1_suggestion_candidates": sum(row["suggestion_candidates"] for row in resolver_records),
        "wave1_guarded_no_apply": sum(row["guarded_no_apply"] for row in resolver_records),
        "wave1_blocked_by_token_integrity": sum(row["blocked_by_token_integrity"] for row in resolver_records),
        "wave1_blocked_by_domain_ambiguity": sum(row["blocked_by_domain_ambiguity"] for row in resolver_records),
        "wave1_blocked_by_context": sum(row["blocked_by_context"] for row in resolver_records),
        "wave1_blocked_by_numeric_or_modifier_guard": int(data["script_value_effect"].get("blocked_by_numeric_or_modifier_guard") or 0),
        "wave1_numeric_modifier_terminalized": int(micro.get("terminal_guard_count") or 0),
        "wave1_false_safe_risk_count": sum(row["false_safe_risk_count"] for row in resolver_records)
        + int(audit.get("false_safe_risk_count") or 0)
        + int(micro.get("false_safe_risk_count") or 0),
        "wave1_requires_apply_later_count": sum(row["requires_apply_later_count"] for row in resolver_records)
        + int(audit.get("requires_apply_later_count") or 0)
        + int(micro.get("requires_apply_later_count") or 0),
        "wave1_requires_lifecycle_later_count": sum(row["requires_lifecycle_later_count"] for row in resolver_records)
        + int(audit.get("requires_lifecycle_later_count") or 0)
        + int(micro.get("requires_lifecycle_later_count") or 0),
        "wave1_would_change_output": sum(row["would_change_output"] for row in resolver_records),
        "wave1_terminal_guard_confirmed": 54 + 61 + int(micro.get("terminal_guard_count") or 0),
        "wave1_reuse_guard_confirmed": 240 + 17 + int(data["script_value_effect"].get("guarded_no_apply_reuse_policy") or 0),
        "wave1_micro_policy_cataloged_only": micro.get("register_component_now") is False,
        "apply_future_recommended_now": False,
        "production_full_recommended_now": False,
        "network_update_now": False,
        "network_update_future_data_only": True,
        "next_prompt": next_prompt,
        "next_prompt_rationale": rationale,
    }
    return summary, records


def write_outputs(summary: dict[str, Any], records: list[dict[str, Any]]) -> tuple[Path, Path, Path]:
    txt_path, jsonl_path, summary_path = output_paths()
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        handle.write(json.dumps({"record_type": "summary", **summary}, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    metric_keys = [
        "wave1_total_reviewed",
        "wave1_resolver_total_reviewed",
        "wave1_suggestion_candidates",
        "wave1_guarded_no_apply",
        "wave1_blocked_by_token_integrity",
        "wave1_blocked_by_domain_ambiguity",
        "wave1_blocked_by_context",
        "wave1_blocked_by_numeric_or_modifier_guard",
        "wave1_numeric_modifier_terminalized",
        "wave1_false_safe_risk_count",
        "wave1_requires_apply_later_count",
        "wave1_requires_lifecycle_later_count",
        "wave1_would_change_output",
        "wave1_terminal_guard_confirmed",
        "wave1_reuse_guard_confirmed",
        "wave1_micro_policy_cataloged_only",
    ]
    lines = [
        "resolver wave 1 consolidated diagnostic",
        f"segment_state_run_id={summary['segment_state_run_id']}",
        f"ledger_run_id={summary['ledger_run_id']}",
        "",
        *[f"{key}={summary[key]}" for key in metric_keys],
        "",
        "analysis:",
        "1. A onda 1 confirmou seguranca dos resolvers: sim, todos ficaram com false_safe=0, apply=0 e lifecycle=0.",
        "2. Candidatos de alteracao: nenhum.",
        "3. Valor mesmo com suggestion_candidates=0: a onda prova que esses blocos sao guards/reuso e devem ser excluidos de tentativas agressivas.",
        "4. Apply futuro agora: nao, porque nao houve proposta de mudanca.",
        "5. Nova policy registrada: nao; a numeric/modifier fica catalogada apenas dentro de script_value_effect_policy por baixa volumetria.",
        f"6. Proxima onda recomendada: {summary['next_prompt']} ({summary['next_prompt_rationale']}).",
        "7. Producao full agora: nao.",
        "8. Network agora: sem redesign; no maximo data-only futuro com contadores de onda dry-run.",
        "",
        "production_full_recommended=false",
        "network_update_now=false",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    parser.add_argument("--ledger-run-id", required=True, type=int)
    args = parser.parse_args()
    data = load_inputs()
    validate_inputs(data, args.segment_state_run_id, args.ledger_run_id)
    summary, records = build_summary(data)
    expected = {
        "wave1_total_reviewed": 615,
        "wave1_suggestion_candidates": 0,
        "wave1_guarded_no_apply": 609,
        "wave1_blocked_by_numeric_or_modifier_guard": 3,
        "wave1_numeric_modifier_terminalized": 3,
        "wave1_false_safe_risk_count": 0,
        "wave1_requires_apply_later_count": 0,
        "wave1_requires_lifecycle_later_count": 0,
    }
    divergences = {
        key: {"expected": value, "actual": summary[key]}
        for key, value in expected.items()
        if summary[key] != value
    }
    summary["expected_divergences"] = divergences
    txt_path, jsonl_path, summary_path = write_outputs(summary, records)
    print(f"txt: {txt_path}")
    print(f"jsonl: {jsonl_path}")
    print(f"summary: {summary_path}")
    for key in [
        "wave1_total_reviewed",
        "wave1_suggestion_candidates",
        "wave1_guarded_no_apply",
        "wave1_blocked_by_numeric_or_modifier_guard",
        "wave1_numeric_modifier_terminalized",
        "wave1_false_safe_risk_count",
        "wave1_requires_apply_later_count",
        "wave1_requires_lifecycle_later_count",
        "next_prompt",
    ]:
        print(f"{key}: {summary[key]}")
    if divergences:
        print(f"expected_divergences: {json.dumps(divergences, ensure_ascii=False, sort_keys=True)}")


if __name__ == "__main__":
    main()
