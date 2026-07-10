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
    "wave1": "reports/20260623_201232_787624_resolver_wave1_consolidated_diagnostic_summary.json",
    "gender_local_player": "reports/20260623_203920_705453_effect_list_gender_local_player_resolver_dry_run_summary.json",
    "trait_accolade": "reports/20260623_213254_535464_effect_list_trait_accolade_resolver_dry_run_summary.json",
    "architecture_summary": "reports/20260623_164747_499852_global_final_architecture_before_resolution_diagnostic_summary.json",
    "architecture_inventory": "reports/20260623_164747_499852_global_final_architecture_before_resolution_diagnostic_inventory.json",
}


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_resolver_wave2_consolidated_diagnostic"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), reports_dir / f"{base.name}_summary.json"


def load_inputs() -> tuple[dict[str, dict[str, Any]], str]:
    data: dict[str, dict[str, Any]] = {}
    for key, rel_path in INPUTS.items():
        path = db.project_path(rel_path)
        if key == "architecture_summary" and not path.exists():
            continue
        if not path.exists():
            raise SystemExit(f"missing required artifact: {path}")
        data[key] = read_json(path)
    architecture_source = "summary_json"
    if "architecture_summary" not in data:
        data["architecture_summary"] = data["architecture_inventory"].get("summary") or {}
        architecture_source = "inventory_json_summary"
    return data, architecture_source


def require_int(row: dict[str, Any], key: str, expected: int, label: str) -> None:
    actual = int(row.get(key) or 0)
    if actual != expected:
        raise SystemExit(f"{label}.{key} expected {expected}, got {actual}")


def validate_inputs(data: dict[str, dict[str, Any]], segment_state_run_id: int, ledger_run_id: int) -> None:
    if segment_state_run_id != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("segment_state_run_id argument guard failed")
    if ledger_run_id != EXPECTED_LEDGER_RUN_ID:
        raise SystemExit("ledger_run_id argument guard failed")

    wave1 = data["wave1"]
    expected_wave1 = {
        "wave1_total_reviewed": 615,
        "wave1_suggestion_candidates": 0,
        "wave1_guarded_no_apply": 609,
        "wave1_blocked_by_numeric_or_modifier_guard": 3,
        "wave1_numeric_modifier_terminalized": 3,
        "wave1_false_safe_risk_count": 0,
        "wave1_requires_apply_later_count": 0,
        "wave1_requires_lifecycle_later_count": 0,
    }
    for key, expected in expected_wave1.items():
        require_int(wave1, key, expected, "wave1")

    expected_resolvers = {
        "gender_local_player": (55, 0, 55),
        "trait_accolade": (46, 0, 46),
    }
    for key, (reviewed, candidates, guarded) in expected_resolvers.items():
        row = data[key]
        require_int(row, "total_reviewed", reviewed, key)
        require_int(row, "suggestion_candidates", candidates, key)
        require_int(row, "guarded_no_apply", guarded, key)
        require_int(row, "false_safe_risk_count", 0, key)
        require_int(row, "requires_apply_later_count", 0, key)
        require_int(row, "requires_lifecycle_later_count", 0, key)

    architecture = data["architecture_summary"]
    require_int(architecture, "registered_agents", 238, "architecture")
    require_int(architecture, "observed_agent_keys", 295, "architecture")
    require_int(architecture, "issue_network_agents", 74, "architecture")
    require_int(architecture, "coverage_after_blocked_uncertain_projected", 11690, "architecture")
    require_int(architecture, "segments_without_useful_spec_projected", 35, "architecture")


def build_records(data: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    wave1 = data["wave1"]
    gender = data["gender_local_player"]
    trait = data["trait_accolade"]
    return [
        {
            "record_type": "wave1_consolidated",
            "reviewed": int(wave1["wave1_total_reviewed"]),
            "suggestion_candidates": int(wave1["wave1_suggestion_candidates"]),
            "guarded_no_apply": int(wave1["wave1_guarded_no_apply"]),
            "blocked_numeric_then_terminalized": int(wave1["wave1_numeric_modifier_terminalized"]),
            "false_safe_risk_count": int(wave1["wave1_false_safe_risk_count"]),
            "requires_apply_later_count": int(wave1["wave1_requires_apply_later_count"]),
            "requires_lifecycle_later_count": int(wave1["wave1_requires_lifecycle_later_count"]),
        },
        {
            "record_type": "wave2_resolver",
            "resolver_key": gender["resolver_key"],
            "source_policy": gender["source_policy"],
            "reviewed": int(gender["total_reviewed"]),
            "suggestion_candidates": int(gender["suggestion_candidates"]),
            "guarded_no_apply": int(gender["guarded_no_apply"]),
            "false_safe_risk_count": int(gender["false_safe_risk_count"]),
            "requires_apply_later_count": int(gender["requires_apply_later_count"]),
            "requires_lifecycle_later_count": int(gender["requires_lifecycle_later_count"]),
        },
        {
            "record_type": "wave2_resolver",
            "resolver_key": trait["resolver_key"],
            "source_policy": trait["source_policy"],
            "reviewed": int(trait["total_reviewed"]),
            "suggestion_candidates": int(trait["suggestion_candidates"]),
            "guarded_no_apply": int(trait["guarded_no_apply"]),
            "false_safe_risk_count": int(trait["false_safe_risk_count"]),
            "requires_apply_later_count": int(trait["requires_apply_later_count"]),
            "requires_lifecycle_later_count": int(trait["requires_lifecycle_later_count"]),
        },
    ]


def build_summary(data: dict[str, dict[str, Any]], architecture_source: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    records = build_records(data)
    reviewed = sum(row["reviewed"] for row in records)
    candidates = sum(row["suggestion_candidates"] for row in records)
    guarded = sum(row["guarded_no_apply"] for row in records)
    false_safe = sum(row["false_safe_risk_count"] for row in records)
    apply_later = sum(row["requires_apply_later_count"] for row in records)
    lifecycle_later = sum(row["requires_lifecycle_later_count"] for row in records)
    numeric_terminalized = int(data["wave1"]["wave1_numeric_modifier_terminalized"])
    next_prompt = "chat_exec_semantic_short_label_autofix_candidate_discovery_prompt.md"
    rationale = (
        "zero candidatos em 716 revisoes indica que resolvers puramente guardados estao protegendo bem, "
        "mas nao descobrem mudancas; para buscar candidatos reais, a proxima etapa deve comparar sinais lexical/semanticos assistidos"
    )
    summary = {
        "schema_version": 1,
        "source": "resolver_wave2_consolidated_diagnostic_v1",
        "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
        "ledger_run_id": EXPECTED_LEDGER_RUN_ID,
        "architecture_source_used": architecture_source,
        "coverage_projected": "11690/11725",
        "segments_without_useful_spec": 35,
        "registered_agents": 238,
        "observed_agent_keys": 295,
        "issue_network_agents": 74,
        "wave1_plus_wave2_reviewed": reviewed,
        "wave1_plus_wave2_suggestion_candidates": candidates,
        "wave1_plus_wave2_guarded_no_apply": guarded,
        "wave1_plus_wave2_blocked_numeric_then_terminalized": numeric_terminalized,
        "wave1_plus_wave2_false_safe_risk_count": false_safe,
        "wave1_plus_wave2_requires_apply_later_count": apply_later,
        "wave1_plus_wave2_requires_lifecycle_later_count": lifecycle_later,
        "guarded_resolver_strategy_still_safe": True,
        "guarded_resolver_strategy_likely_to_find_candidates": False,
        "apply_future_recommended_now": False,
        "production_full_recommended_now": False,
        "network_update_now": False,
        "network_update_future_data_only": True,
        "next_prompt": next_prompt,
        "next_prompt_rationale": rationale,
    }
    expected = {
        "wave1_plus_wave2_reviewed": 716,
        "wave1_plus_wave2_suggestion_candidates": 0,
        "wave1_plus_wave2_guarded_no_apply": 710,
        "wave1_plus_wave2_blocked_numeric_then_terminalized": 3,
        "wave1_plus_wave2_false_safe_risk_count": 0,
        "wave1_plus_wave2_requires_apply_later_count": 0,
        "wave1_plus_wave2_requires_lifecycle_later_count": 0,
    }
    summary["expected_divergences"] = {
        key: {"expected": expected_value, "actual": summary[key]}
        for key, expected_value in expected.items()
        if summary[key] != expected_value
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
        "wave1_plus_wave2_reviewed",
        "wave1_plus_wave2_suggestion_candidates",
        "wave1_plus_wave2_guarded_no_apply",
        "wave1_plus_wave2_blocked_numeric_then_terminalized",
        "wave1_plus_wave2_false_safe_risk_count",
        "wave1_plus_wave2_requires_apply_later_count",
        "wave1_plus_wave2_requires_lifecycle_later_count",
    ]
    divergence_note = (
        "Sem divergencias contra os valores esperados."
        if not summary["expected_divergences"]
        else f"Divergencias: {json.dumps(summary['expected_divergences'], ensure_ascii=False, sort_keys=True)}"
    )
    lines = [
        "resolver wave 2 consolidated diagnostic",
        f"segment_state_run_id={summary['segment_state_run_id']}",
        f"ledger_run_id={summary['ledger_run_id']}",
        f"architecture_source_used={summary['architecture_source_used']}",
        "",
        *[f"{key}={summary[key]}" for key in metric_keys],
        f"expected_divergences={summary['expected_divergences']}",
        "",
        "analysis:",
        "1. As ondas 1 e 2 confirmaram seguranca: sim, false-safe=0, apply=0 e lifecycle=0.",
        "2. Candidato real gerado: nenhum.",
        "3. Aprendizado: os blocos roteados de maior cobertura estao bem protegidos por reuso/guard e devem ser preservados por default.",
        "4. A abordagem de resolver guardado continua util para reduzir risco, mas nao deve continuar igual se a meta for encontrar candidatos.",
        "5. Para buscar candidatos, usar descoberta lexical/semantica assistida em dry-run, com comparacao controlada e auditoria humana antes de qualquer apply.",
        "6. Producao full agora: nao; ainda mediria uma arquitetura que nao produz candidatos.",
        "7. Network agora: sem redesign; data-only futuro pode expor contadores de wave1/wave2.",
        divergence_note,
        "",
        f"next_prompt={summary['next_prompt']}",
        f"next_prompt_rationale={summary['next_prompt_rationale']}",
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
    data, architecture_source = load_inputs()
    validate_inputs(data, args.segment_state_run_id, args.ledger_run_id)
    summary, records = build_summary(data, architecture_source)
    txt_path, jsonl_path, summary_path = write_outputs(summary, records)
    print(f"txt: {txt_path}")
    print(f"jsonl: {jsonl_path}")
    print(f"summary: {summary_path}")
    for key in [
        "wave1_plus_wave2_reviewed",
        "wave1_plus_wave2_suggestion_candidates",
        "wave1_plus_wave2_guarded_no_apply",
        "wave1_plus_wave2_blocked_numeric_then_terminalized",
        "wave1_plus_wave2_false_safe_risk_count",
        "wave1_plus_wave2_requires_apply_later_count",
        "wave1_plus_wave2_requires_lifecycle_later_count",
        "next_prompt",
    ]:
        print(f"{key}: {summary[key]}")
    if summary["expected_divergences"]:
        print(f"expected_divergences: {json.dumps(summary['expected_divergences'], ensure_ascii=False, sort_keys=True)}")


if __name__ == "__main__":
    main()
