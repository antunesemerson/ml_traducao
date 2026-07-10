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
    "architecture_jsonl": "reports/20260623_164747_499852_global_final_architecture_before_resolution_diagnostic.jsonl",
    "architecture_inventory": "reports/20260623_164747_499852_global_final_architecture_before_resolution_diagnostic_inventory.json",
    "wave2": "reports/20260623_213737_409531_resolver_wave2_consolidated_diagnostic_summary.json",
    "semantic_discovery": "reports/20260623_220129_729915_semantic_short_label_autofix_candidate_discovery_summary.json",
    "remaining35": "reports/20260623_221314_539712_remaining_35_final_review_summary.json",
    "tenet_spec": "reports/20260623_223126_231401_remaining_religion_culture_tenet_policy_review_spec.json",
}


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_resolution_phase_status_and_next_strategy"
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


def load_inputs() -> dict[str, Any]:
    data: dict[str, Any] = {}
    for key, rel_path in INPUTS.items():
        path = db.project_path(rel_path)
        if not path.exists():
            raise SystemExit(f"missing required artifact: {path}")
        data[key] = read_jsonl(path) if key == "architecture_jsonl" else read_json(path)
    return data


def summary_from_architecture_jsonl(rows: list[dict[str, Any]]) -> dict[str, Any]:
    summaries = [row for row in rows if row.get("record_type") == "summary" and "pending_total" in row]
    if len(summaries) != 1:
        raise SystemExit(f"architecture summary guard failed: {len(summaries)}")
    return summaries[0]


def require_int(row: dict[str, Any], key: str, expected: int, label: str) -> None:
    actual = int(row.get(key) or 0)
    if actual != expected:
        raise SystemExit(f"{label}.{key} expected {expected}, got {actual}")


def validate(data: dict[str, Any], segment_state_run_id: int, ledger_run_id: int) -> dict[str, Any]:
    if segment_state_run_id != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("segment_state_run_id argument guard failed")
    if ledger_run_id != EXPECTED_LEDGER_RUN_ID:
        raise SystemExit("ledger_run_id argument guard failed")

    architecture = summary_from_architecture_jsonl(data["architecture_jsonl"])
    inventory_summary = data["architecture_inventory"].get("summary") or {}
    wave2 = data["wave2"]
    discovery = data["semantic_discovery"]
    remaining = data["remaining35"]
    tenet = data["tenet_spec"]

    for source, label in [(architecture, "architecture"), (inventory_summary, "inventory")]:
        require_int(source, "segment_state_run_id", 400, label)
        require_int(source, "ledger_run_id", 76, label)
        require_int(source, "registered_agents", 238, label)
        require_int(source, "observed_agent_keys", 295, label)
        require_int(source, "issue_network_agents", 74, label)
        require_int(source, "coverage_after_blocked_uncertain_projected", 11690, label)
        require_int(source, "segments_without_useful_spec_projected", 35, label)

    require_int(wave2, "wave1_plus_wave2_reviewed", 716, "wave2")
    require_int(wave2, "wave1_plus_wave2_suggestion_candidates", 0, "wave2")
    require_int(wave2, "wave1_plus_wave2_false_safe_risk_count", 0, "wave2")
    require_int(wave2, "wave1_plus_wave2_requires_apply_later_count", 0, "wave2")
    require_int(wave2, "wave1_plus_wave2_requires_lifecycle_later_count", 0, "wave2")

    require_int(discovery, "total_reviewed", 240, "semantic_discovery")
    require_int(discovery, "candidate_count", 8, "semantic_discovery")
    require_int(discovery, "candidate_semantic_minor_lexical_repair", 6, "semantic_discovery")
    require_int(discovery, "candidate_spacing_punctuation_cleanup", 2, "semantic_discovery")
    require_int(discovery, "false_safe_risk_count", 0, "semantic_discovery")
    require_int(discovery, "requires_apply_later_count", 0, "semantic_discovery")
    require_int(discovery, "requires_lifecycle_later_count", 0, "semantic_discovery")

    require_int(remaining, "total_reviewed", 35, "remaining35")
    require_int(remaining, "remaining_true_unknown", 0, "remaining35")
    require_int(remaining, "remaining_religion_culture_tenet_policy", 19, "remaining35")
    require_int(remaining, "remaining_language_residual_policy", 6, "remaining35")
    require_int(remaining, "remaining_name_title_culture_policy", 1, "remaining35")
    require_int(remaining, "false_safe_risk_count", 0, "remaining35")

    require_int(tenet, "total_reviewed", 19, "tenet")
    require_int(tenet, "tenet_terminal_guard_with_religion_domain", 19, "tenet")
    require_int(tenet, "register_component_now_count", 0, "tenet")
    require_int(tenet, "false_safe_risk_count", 0, "tenet")

    return {
        "architecture": architecture,
        "inventory": inventory_summary,
        "wave2": wave2,
        "discovery": discovery,
        "remaining": remaining,
        "tenet": tenet,
    }


def build_summary(validated: dict[str, Any]) -> dict[str, Any]:
    discovery = validated["discovery"]
    resolver = validated["wave2"]
    remaining = validated["remaining"]
    candidate_rate = round(float(discovery["candidate_count"]) / float(discovery["total_reviewed"]), 6)
    summary = {
        "schema_version": 1,
        "source": "resolution_phase_status_and_next_strategy_v1",
        "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
        "ledger_run_id": EXPECTED_LEDGER_RUN_ID,
        "architecture_closed": True,
        "true_unknown_count": int(remaining["remaining_true_unknown"]),
        "known_leftovers_count": int(remaining["total_reviewed"]),
        "resolver_reviewed_total": int(resolver["wave1_plus_wave2_reviewed"]),
        "resolver_suggestion_candidates": int(resolver["wave1_plus_wave2_suggestion_candidates"]),
        "resolver_false_safe_risk_count": int(resolver["wave1_plus_wave2_false_safe_risk_count"]),
        "semantic_discovery_reviewed": int(discovery["total_reviewed"]),
        "semantic_discovery_candidates": int(discovery["candidate_count"]),
        "semantic_discovery_candidate_rate": candidate_rate,
        "apply_ready_now": 0,
        "production_full_recommended_now": False,
        "network_update_recommended": False,
        "network_update_data_only_optional": True,
        "model_training_recommended": False,
        "recommended_next_prompt": "chat_exec_semantic_short_label_autofix_candidate_audit_prompt.md",
        "recommended_next_prompt_rationale": "auditar os 8 candidatos testa qualidade antes de ampliar a descoberta lexical/semantica",
        "alternative_next_prompt": "chat_exec_semantic_short_label_autofix_candidate_discovery_expand_prompt.md",
    }
    return summary


def write_outputs(summary: dict[str, Any], validated: dict[str, Any]) -> tuple[Path, Path, Path]:
    txt_path, jsonl_path, summary_path = output_paths()
    records = [
        {
            "record_type": "architecture_status",
            "requirement_effect_route": "matured",
            "not_requirement_effect_route": "matured",
            "domain_context_route": "matured",
            "blocked_uncertain_route": "matured",
            "remaining_35": "known_leftovers_no_true_unknown",
            "remaining_tenet": "terminal_guard_cataloged",
        },
        {
            "record_type": "resolver_status",
            "reviewed": summary["resolver_reviewed_total"],
            "suggestion_candidates": summary["resolver_suggestion_candidates"],
            "false_safe_risk_count": summary["resolver_false_safe_risk_count"],
            "apply_ready_now": summary["apply_ready_now"],
        },
        {
            "record_type": "semantic_discovery_status",
            "reviewed": summary["semantic_discovery_reviewed"],
            "candidate_count": summary["semantic_discovery_candidates"],
            "candidate_rate": summary["semantic_discovery_candidate_rate"],
            "sample_candidate_ids": validated["discovery"].get("sample_candidate_ids", []),
        },
        {
            "record_type": "summary",
            **summary,
        },
    ]
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "resolution phase status and next strategy",
        f"segment_state_run_id={summary['segment_state_run_id']}",
        f"ledger_run_id={summary['ledger_run_id']}",
        "",
        f"architecture_closed={str(summary['architecture_closed']).lower()}",
        f"true_unknown_count={summary['true_unknown_count']}",
        f"known_leftovers_count={summary['known_leftovers_count']}",
        f"resolver_reviewed_total={summary['resolver_reviewed_total']}",
        f"resolver_suggestion_candidates={summary['resolver_suggestion_candidates']}",
        f"resolver_false_safe_risk_count={summary['resolver_false_safe_risk_count']}",
        f"semantic_discovery_reviewed={summary['semantic_discovery_reviewed']}",
        f"semantic_discovery_candidates={summary['semantic_discovery_candidates']}",
        f"semantic_discovery_candidate_rate={summary['semantic_discovery_candidate_rate']}",
        f"apply_ready_now={summary['apply_ready_now']}",
        f"production_full_recommended_now={str(summary['production_full_recommended_now']).lower()}",
        f"network_update_recommended={str(summary['network_update_recommended']).lower()}",
        f"model_training_recommended={str(summary['model_training_recommended']).lower()}",
        "",
        "analysis:",
        "1. A arquitetura de roteamento pode ser considerada fechada: sim, nao ha true_unknown.",
        "2. O que resta sem spec util/desconhecido: nenhum desconhecido; os 35 sao leftovers conhecidos e o tenet foi catalogado como terminal guard.",
        "3. Ondas de resolvers geraram candidatos aplicaveis: nao, 716 revisoes deram 0 candidatos.",
        "4. Aprendizado dos 716 dry-runs: os blocos estruturais estao seguros, mas resolvers guardados nao geram ganho real sozinhos.",
        "5. Aprendizado dos 8 candidatos lexicais: existe sinal real, mas pequeno; precisa auditoria de qualidade antes de escalar.",
        "6. Proximo melhor caminho: auditar os 8 candidatos atuais antes de ampliar descoberta.",
        "7. Producao full agora: nao.",
        "8. Network agora: nao redesenhar; data-only opcional para architecture_closed=true, true_unknown=0, resolver_candidates=8, apply_ready=0.",
        "9. Rede neural/modelo: sem treino/model promotion agora; gargalo e estrategia de candidato + auditoria.",
        "",
        f"recommended_next_prompt={summary['recommended_next_prompt']}",
        f"recommended_next_prompt_rationale={summary['recommended_next_prompt_rationale']}",
        f"alternative_next_prompt={summary['alternative_next_prompt']}",
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
    validated = validate(data, args.segment_state_run_id, args.ledger_run_id)
    summary = build_summary(validated)
    txt_path, jsonl_path, summary_path = write_outputs(summary, validated)
    print(f"txt: {txt_path}")
    print(f"jsonl: {jsonl_path}")
    print(f"summary: {summary_path}")
    for key in [
        "architecture_closed",
        "true_unknown_count",
        "known_leftovers_count",
        "resolver_reviewed_total",
        "resolver_suggestion_candidates",
        "semantic_discovery_reviewed",
        "semantic_discovery_candidates",
        "apply_ready_now",
        "production_full_recommended_now",
        "model_training_recommended",
        "recommended_next_prompt",
    ]:
        print(f"{key}: {summary[key]}")


if __name__ == "__main__":
    main()
