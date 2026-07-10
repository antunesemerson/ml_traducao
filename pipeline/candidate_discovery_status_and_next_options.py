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
    "semantic_autofix_initial": "reports/20260623_220129_729915_semantic_short_label_autofix_candidate_discovery_summary.json",
    "semantic_autofix_audit": "reports/20260623_231628_997455_semantic_short_label_autofix_candidate_audit_summary.json",
    "semantic_autofix_expand": "reports/20260623_233604_073950_semantic_short_label_autofix_candidate_discovery_expand_summary.json",
    "retarget": "reports/20260624_004036_196562_candidate_discovery_cohort_retarget_summary.json",
    "short_label_initial": "reports/20260624_014300_217906_short_label_clean_candidate_discovery_summary.json",
    "short_label_audit": "reports/20260624_021400_066289_short_label_clean_candidate_small_audit_summary.json",
    "short_label_expand": "reports/20260624_023918_807449_short_label_clean_candidate_discovery_expand_summary.json",
    "semantic_single": "reports/20260624_025746_762270_semantic_single_family_candidate_discovery_summary.json",
    "resolution_status": "reports/20260623_225655_764509_resolution_phase_status_and_next_strategy_summary.json",
}


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_candidate_discovery_status_and_next_options"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), reports_dir / f"{base.name}_summary.json"


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_inputs() -> dict[str, dict[str, Any]]:
    loaded: dict[str, dict[str, Any]] = {}
    for key, rel_path in INPUTS.items():
        path = db.project_path(rel_path)
        if not path.exists():
            raise SystemExit(f"missing required summary: {path}")
        loaded[key] = read_json(path)
    return loaded


def require_int(row: dict[str, Any], key: str, expected: int, label: str) -> None:
    actual = int(row.get(key) or 0)
    if actual != expected:
        raise SystemExit(f"{label}.{key} expected {expected}, got {actual}")


def validate(data: dict[str, dict[str, Any]], segment_state_run_id: int, ledger_run_id: int) -> None:
    if segment_state_run_id != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("segment_state_run_id argument guard failed")
    if ledger_run_id != EXPECTED_LEDGER_RUN_ID:
        raise SystemExit("ledger_run_id argument guard failed")
    require_int(data["semantic_autofix_initial"], "total_reviewed", 240, "semantic_autofix_initial")
    require_int(data["semantic_autofix_initial"], "candidate_count", 8, "semantic_autofix_initial")
    require_int(data["semantic_autofix_audit"], "safe_for_future_apply_batch_count", 8, "semantic_autofix_audit")
    require_int(data["semantic_autofix_expand"], "total_reviewed", 600, "semantic_autofix_expand")
    require_int(data["semantic_autofix_expand"], "candidate_count", 0, "semantic_autofix_expand")
    require_int(data["short_label_initial"], "total_universe", 741, "short_label_initial")
    require_int(data["short_label_initial"], "total_reviewed", 600, "short_label_initial")
    require_int(data["short_label_initial"], "candidate_count", 6, "short_label_initial")
    require_int(data["short_label_audit"], "safe_for_future_apply_batch_count", 6, "short_label_audit")
    require_int(data["short_label_expand"], "new_reviewed_count", 141, "short_label_expand")
    require_int(data["short_label_expand"], "new_candidate_count", 0, "short_label_expand")
    require_int(data["semantic_single"], "total_reviewed", 360, "semantic_single")
    require_int(data["semantic_single"], "candidate_count", 3, "semantic_single")
    if not bool(data["resolution_status"].get("architecture_closed")):
        raise SystemExit("architecture_closed guard failed")
    require_int(data["resolution_status"], "true_unknown_count", 0, "resolution_status")
    for label, row in data.items():
        if int(row.get("false_safe_risk_count") or 0) != 0:
            raise SystemExit(f"{label} false-safe guard failed")
        if int(row.get("requires_lifecycle_later_count") or 0) != 0:
            raise SystemExit(f"{label} lifecycle guard failed")


def build_summary(data: dict[str, dict[str, Any]]) -> dict[str, Any]:
    reviewed = (
        int(data["semantic_autofix_initial"]["total_reviewed"])
        + int(data["semantic_autofix_expand"]["total_reviewed"])
        + int(data["short_label_initial"]["total_reviewed"])
        + int(data["short_label_expand"]["new_reviewed_count"])
        + int(data["semantic_single"]["total_reviewed"])
    )
    total_raw = (
        int(data["semantic_autofix_initial"]["candidate_count"])
        + int(data["semantic_autofix_expand"]["candidate_count"])
        + int(data["short_label_initial"]["candidate_count"])
        + int(data["short_label_expand"]["new_candidate_count"])
        + int(data["semantic_single"]["candidate_count"])
    )
    audited = int(data["semantic_autofix_audit"]["total_candidates_audited"]) + int(data["short_label_audit"]["total_candidates_audited"])
    accepts = int(data["semantic_autofix_audit"]["safe_for_future_apply_batch_count"]) + int(data["short_label_audit"]["safe_for_future_apply_batch_count"])
    candidate_rates = {
        "semantic_short_label_autofix_initial": round(int(data["semantic_autofix_initial"]["candidate_count"]) / int(data["semantic_autofix_initial"]["total_reviewed"]), 6),
        "semantic_short_label_autofix_expand": 0.0,
        "short_label_clean_initial": round(int(data["short_label_initial"]["candidate_count"]) / int(data["short_label_initial"]["total_reviewed"]), 6),
        "short_label_clean_expand": 0.0,
        "semantic_single_family": round(int(data["semantic_single"]["candidate_count"]) / int(data["semantic_single"]["total_reviewed"]), 6),
    }
    best_rate = max(candidate_rates, key=candidate_rates.get)
    absolute_counts = {
        "semantic_short_label_autofix_initial": int(data["semantic_autofix_initial"]["candidate_count"]),
        "semantic_short_label_autofix_expand": int(data["semantic_autofix_expand"]["candidate_count"]),
        "short_label_clean_initial": int(data["short_label_initial"]["candidate_count"]),
        "short_label_clean_expand": int(data["short_label_expand"]["new_candidate_count"]),
        "semantic_single_family": int(data["semantic_single"]["candidate_count"]),
    }
    best_absolute = max(absolute_counts, key=absolute_counts.get)
    summary = {
        "schema_version": 1,
        "source": "candidate_discovery_status_and_next_options_v1",
        "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
        "ledger_run_id": EXPECTED_LEDGER_RUN_ID,
        "architecture_closed": True,
        "true_unknown_count": 0,
        "total_reviewed_across_discoveries": reviewed,
        "total_raw_candidates": total_raw,
        "total_audited_candidates": audited,
        "total_audited_accepts": accepts,
        "semantic_single_family_unaudited_candidates": int(data["semantic_single"]["candidate_count"]),
        "candidate_rate_overall": round(total_raw / reviewed, 6),
        "best_candidate_rate_cohort": best_rate,
        "best_absolute_candidate_cohort": best_absolute,
        "false_safe_risk_total": 0,
        "apply_ready_now": 0,
        "production_full_recommended_now": False,
        "recommended_option": "audit_semantic_single_family_candidates",
        "recommended_next_prompt": "chat_exec_semantic_single_family_candidate_small_audit_prompt.md",
        "network_update_recommended": False,
        "network_update_data_only_optional": True,
        "model_training_recommended": False,
    }
    if summary["total_raw_candidates"] != 17:
        raise SystemExit("total_raw_candidates guard failed")
    if summary["total_audited_accepts"] != 14:
        raise SystemExit("total_audited_accepts guard failed")
    if summary["semantic_single_family_unaudited_candidates"] != 3:
        raise SystemExit("semantic_single unaudited guard failed")
    return summary


def build_options(summary: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "option_key": "audit_semantic_single_family_candidates",
            "expected_gain": "medium",
            "risk": "low",
            "recommended": True,
            "reason": "fecha o lote conhecido de 17 candidatos antes de nova busca; 3 pendentes, false-safe global 0",
            "next_prompt": "chat_exec_semantic_single_family_candidate_small_audit_prompt.md",
        },
        {
            "option_key": "expand_semantic_single_family",
            "expected_gain": "low",
            "risk": "low",
            "recommended": False,
            "reason": "universo semantic_single_family ja foi todo revisado",
            "next_prompt": "",
        },
        {
            "option_key": "retarget_autofix_unknown_single_family",
            "expected_gain": "low",
            "risk": "low",
            "recommended": False,
            "reason": "retarget anterior mediu baixa superficie literal; melhor fechar os 3 pendentes primeiro",
            "next_prompt": "chat_exec_candidate_discovery_retarget_second_pass_prompt.md",
        },
        {
            "option_key": "retarget_culture_semantic_single_family",
            "expected_gain": "low",
            "risk": "medium",
            "recommended": False,
            "reason": "cohort tende a exigir contexto de cultura/nome; maior risco sem heuristica nova",
            "next_prompt": "chat_exec_candidate_discovery_retarget_second_pass_prompt.md",
        },
        {
            "option_key": "pause_discovery_for_heuristic_plan",
            "expected_gain": "medium",
            "risk": "low",
            "recommended": False,
            "reason": "faz sentido depois de auditar os 3 restantes e consolidar o lote conhecido",
            "next_prompt": "chat_exec_candidate_discovery_heuristic_plan_prompt.md",
        },
        {
            "option_key": "run_full_production",
            "expected_gain": "low",
            "risk": "high",
            "recommended": False,
            "reason": "apply_ready_now=0; candidatos ainda sao apenas material auditado/pendente, nao lote aplicado",
            "next_prompt": "",
        },
    ]


def write_outputs(options: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    txt_path, jsonl_path, summary_path = output_paths()
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for option in options:
            handle.write(json.dumps(option, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    metric_keys = [
        "architecture_closed",
        "true_unknown_count",
        "total_reviewed_across_discoveries",
        "total_raw_candidates",
        "total_audited_candidates",
        "total_audited_accepts",
        "semantic_single_family_unaudited_candidates",
        "candidate_rate_overall",
        "best_candidate_rate_cohort",
        "best_absolute_candidate_cohort",
        "false_safe_risk_total",
        "apply_ready_now",
        "production_full_recommended_now",
        "recommended_option",
        "recommended_next_prompt",
        "network_update_recommended",
        "model_training_recommended",
    ]
    lines = [
        "candidate discovery status and next options",
        f"segment_state_run_id={summary['segment_state_run_id']}",
        f"ledger_run_id={summary['ledger_run_id']}",
        "",
        *[f"{key}={summary[key]}" for key in metric_keys],
        "",
        "answers:",
        f"1. Candidatos totais apos arquitetura fechada: {summary['total_raw_candidates']}.",
        f"2. Ja auditados e aceitos: {summary['total_audited_accepts']} de {summary['total_audited_candidates']} auditados.",
        "3. Vale auditar os 3 semantic single-family: true.",
        "4. Ainda vale procurar novos cohorts agora: nao antes de fechar os 3 pendentes.",
        "5. Producao full agora: false.",
        "6. Network precisa atualizar: false; data-only opcional.",
        "7. Treino/model promotion agora: false.",
        f"8. Proximo prompt recomendado: {summary['recommended_next_prompt']}.",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--segment-state-run-id", type=int, required=True)
    parser.add_argument("--ledger-run-id", type=int, required=True)
    args = parser.parse_args()
    data = load_inputs()
    validate(data, args.segment_state_run_id, args.ledger_run_id)
    summary = build_summary(data)
    options = build_options(summary)
    txt_path, jsonl_path, summary_path = write_outputs(options, summary)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    for key in [
        "total_reviewed_across_discoveries",
        "total_raw_candidates",
        "total_audited_candidates",
        "total_audited_accepts",
        "semantic_single_family_unaudited_candidates",
        "false_safe_risk_total",
        "apply_ready_now",
        "recommended_next_prompt",
    ]:
        print(f"{key}={summary[key]}")


if __name__ == "__main__":
    main()
