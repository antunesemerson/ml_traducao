from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import db


EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76
EXPECTED_PENDING_TOTAL = 11725
EXPECTED_ROUTED_COVERAGE = 11690
EXPECTED_WITHOUT_USEFUL_SPEC = 35
EXPECTED_REGISTERED_AGENTS = 238
EXPECTED_OBSERVED_AGENT_KEYS = 295
EXPECTED_OPERATIONAL_AGENTS = 33
EXPECTED_DRY_RUN_AGENTS = 28
EXPECTED_SHADOW_AGENTS = 91
EXPECTED_TERMINAL_GUARD_AGENTS = 22
EXPECTED_SPLITTER_AGENTS = 27
EXPECTED_ISSUE_NETWORK_AGENTS = 74

FINAL_ARCHITECTURE_INVENTORY = Path("reports/20260623_164747_499852_global_final_architecture_before_resolution_diagnostic_inventory.json")

CANDIDATE_KEYS = [
    "effect_list_concept_policy",
    "holy_site_effect_name_policy",
    "script_value_effect_policy",
    "blocked_uncertain_token_integrity_debug_marker_policy",
    "domain_context_landed_title_dynasty_house_name_policy",
    "effect_list_gender_local_player_policy",
    "effect_list_trait_accolade_policy",
    "effect_list_script_value_policy",
    "gender_local_player_policy",
    "select_cstring_requirement_policy",
]

BACKLOG = {
    "blocked_religion_culture_leftovers_projected": 28,
    "needs_blocked_language_residual_policy": 6,
    "needs_blocked_name_title_culture_policy": 1,
}

METRIC_CONTRACT = [
    "total_reviewed",
    "suggestion_candidates",
    "guarded_no_apply",
    "blocked_by_token_integrity",
    "blocked_by_context",
    "blocked_by_gender_perspective",
    "blocked_by_domain_ambiguity",
    "would_change_output",
    "confirmed_matches_output",
    "false_safe_risk_count",
    "requires_apply_later",
    "requires_lifecycle_later",
    "sample_ids",
]

SEGMENT_RECORD_CONTRACT = {
    "segment_id": 0,
    "resolver_key": "",
    "source_policy": "",
    "original_text": "",
    "current_output_text": "",
    "proposed_text": "",
    "decision": "",
    "guards": {},
    "would_change_output": False,
    "requires_apply_later": False,
    "requires_lifecycle_later": False,
    "false_safe_risk": False,
    "notes": "",
}

GLOBAL_GUARDS = {
    "auto_apply_allowed": 0,
    "production_release_allowed": 0,
    "lifecycle_allowed": 0,
    "requires_apply_later_semantics": "measurement_only_never_action",
    "requires_lifecycle_later_semantics": "measurement_only_never_action",
    "preserve": [
        "CK3 dynamic tokens",
        "bracket expressions",
        "formatting tags",
        "variables",
        "Select_CString structure",
        "ES_* helper structure",
        "scope getters",
        "line breaks",
        "debug markers #D ... #!",
    ],
}


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_resolver_dry_run_strategy"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), base.with_name(base.name + "_plan.json")


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def read_json(path: Path) -> dict[str, Any]:
    full_path = db.project_path(str(path))
    with full_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def registry_metrics(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT * FROM ml_agent_registry").fetchall()
    metrics = {
        "registered_agents": len(rows),
        "observed_agent_keys": EXPECTED_OBSERVED_AGENT_KEYS,
        "operational_agents": sum(1 for row in rows if row["operational_state"] == "operational"),
        "dry_run_agents": sum(1 for row in rows if row["operational_state"] == "dry_run"),
        "shadow_agents": sum(1 for row in rows if row["operational_state"] == "shadow"),
        "terminal_guard_agents": sum(1 for row in rows if row["decision_role"] == "terminal_guard"),
        "splitter_agents": sum(1 for row in rows if row["decision_role"] == "route_and_split"),
        "issue_network_agents": sum(1 for row in rows if row["dashboard_group"] == "Issue Network"),
        "requirement_effect_agents": sum(1 for row in rows if row["scope_group"] == "requirement_effect_router"),
        "not_requirement_effect_agents": sum(1 for row in rows if row["scope_group"] == "not_requirement_effect_router"),
        "blocked_uncertain_agents": sum(1 for row in rows if row["scope_group"] == "blocked_uncertain_router"),
        "domain_context_agents": sum(1 for row in rows if "domain_context" in str(row["agent_key"])),
    }
    expected = {
        "registered_agents": EXPECTED_REGISTERED_AGENTS,
        "operational_agents": EXPECTED_OPERATIONAL_AGENTS,
        "dry_run_agents": EXPECTED_DRY_RUN_AGENTS,
        "shadow_agents": EXPECTED_SHADOW_AGENTS,
        "terminal_guard_agents": EXPECTED_TERMINAL_GUARD_AGENTS,
        "splitter_agents": EXPECTED_SPLITTER_AGENTS,
        "issue_network_agents": EXPECTED_ISSUE_NETWORK_AGENTS,
    }
    for key, value in expected.items():
        if metrics[key] != value:
            raise SystemExit(f"registry metric guard failed: {key}={metrics[key]} expected {value}")
    return metrics


def fetch_candidate_notes(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for key in CANDIDATE_KEYS:
        row = conn.execute("SELECT * FROM ml_agent_registry WHERE agent_key = ?", (key,)).fetchone()
        if row is None:
            result[key] = {"registered": False, "notes": {}}
            continue
        notes = json.loads(row["notes_json"] or "{}")
        result[key] = {
            "registered": True,
            "status": row["status"],
            "operational_state": row["operational_state"],
            "agent_type": row["agent_type"],
            "decision_role": row["decision_role"],
            "scope_group": row["scope_group"],
            "notes": notes,
        }
        for flag in ["auto_apply_allowed", "production_release_allowed", "lifecycle_allowed"]:
            if int(notes.get(flag) or 0) != 0:
                raise SystemExit(f"{key} has unsafe {flag}: {notes.get(flag)}")
    return result


def candidate_count(key: str, meta: dict[str, Any]) -> int:
    notes = meta.get("notes") or {}
    if key == "blocked_uncertain_token_integrity_debug_marker_policy":
        return int(notes.get("review_total") or 54)
    if key == "domain_context_landed_title_dynasty_house_name_policy":
        return int(notes.get("review_total") or 61)
    if key == "holy_site_effect_name_policy":
        return int(notes.get("review_total") or 240)
    if key == "script_value_effect_policy":
        return int(notes.get("review_total") or 240)
    if key == "effect_list_concept_policy":
        return int(notes.get("review_total") or 17)
    if key == "effect_list_gender_local_player_policy":
        return int(notes.get("review_total") or 55)
    if key == "effect_list_trait_accolade_policy":
        return int(notes.get("review_total") or 46)
    if key == "effect_list_script_value_policy":
        return int(notes.get("review_total") or 25)
    if key == "select_cstring_requirement_policy":
        return int(notes.get("review_total") or 47)
    if key == "gender_local_player_policy":
        return int(notes.get("review_total") or 0)
    return int(notes.get("review_total") or 0)


def build_candidate_plan(meta_by_key: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    templates = {
        "blocked_uncertain_token_integrity_debug_marker_policy": {
            "terminal_or_reuse_strength": "terminal_54_of_54",
            "token_integrity_risk": "low",
            "perspective_or_gender_risk": "low",
            "domain_context_risk": "low",
            "expected_false_safe_risk": "low",
            "expected_suggestion_yield": "medium",
            "requires_parser": False,
            "requires_context_window": False,
            "resolver_complexity": "small",
            "recommended_wave": 1,
            "rank": 1,
            "resolver_key": "blocked_uncertain_token_integrity_debug_marker_resolver_dry_run",
            "rationale": "terminal route with fixed #D ... #! preservation guard and no semantic rewrite requirement",
        },
        "domain_context_landed_title_dynasty_house_name_policy": {
            "terminal_or_reuse_strength": "terminal_61_of_61",
            "token_integrity_risk": "low",
            "perspective_or_gender_risk": "low",
            "domain_context_risk": "medium",
            "expected_false_safe_risk": "low",
            "expected_suggestion_yield": "medium",
            "requires_parser": False,
            "requires_context_window": True,
            "resolver_complexity": "medium",
            "recommended_wave": 1,
            "rank": 2,
            "resolver_key": "domain_context_landed_title_dynasty_house_name_resolver_dry_run",
            "rationale": "cohesive terminal title/dynasty/house guard, but needs domain context checks",
        },
        "holy_site_effect_name_policy": {
            "terminal_or_reuse_strength": "reuse_240_of_240_effect_list_concept",
            "token_integrity_risk": "low",
            "perspective_or_gender_risk": "low",
            "domain_context_risk": "medium",
            "expected_false_safe_risk": "medium",
            "expected_suggestion_yield": "medium",
            "requires_parser": False,
            "requires_context_window": True,
            "resolver_complexity": "medium",
            "recommended_wave": 2,
            "rank": 3,
            "resolver_key": "holy_site_effect_name_resolver_dry_run",
            "rationale": "full catalog reuse but concept/domain semantics need human-readable guard evidence",
        },
        "effect_list_concept_policy": {
            "terminal_or_reuse_strength": "terminal_reuse_17_of_17",
            "token_integrity_risk": "low",
            "perspective_or_gender_risk": "medium",
            "domain_context_risk": "medium",
            "expected_false_safe_risk": "medium",
            "expected_suggestion_yield": "small",
            "requires_parser": False,
            "requires_context_window": True,
            "resolver_complexity": "medium",
            "recommended_wave": 2,
            "rank": 4,
            "resolver_key": "effect_list_concept_resolver_dry_run",
            "rationale": "terminal reuse route, but concept meaning should remain guarded",
        },
        "script_value_effect_policy": {
            "terminal_or_reuse_strength": "reuse_180_of_240_with_57_residual",
            "token_integrity_risk": "medium",
            "perspective_or_gender_risk": "low",
            "domain_context_risk": "medium",
            "expected_false_safe_risk": "medium",
            "expected_suggestion_yield": "medium",
            "requires_parser": True,
            "requires_context_window": True,
            "resolver_complexity": "large",
            "recommended_wave": 2,
            "rank": 5,
            "resolver_key": "script_value_effect_resolver_dry_run",
            "rationale": "strong reuse but residual ScriptValue branch raises parser and numeric/percent risk",
        },
        "effect_list_script_value_policy": {
            "terminal_or_reuse_strength": "reuse_24_of_25",
            "token_integrity_risk": "medium",
            "perspective_or_gender_risk": "low",
            "domain_context_risk": "low",
            "expected_false_safe_risk": "medium",
            "expected_suggestion_yield": "small",
            "requires_parser": True,
            "requires_context_window": False,
            "resolver_complexity": "medium",
            "recommended_wave": 2,
            "rank": 6,
            "resolver_key": "effect_list_script_value_resolver_dry_run",
            "rationale": "small mostly-reuse block, but ScriptValue preservation gate is mandatory",
        },
        "effect_list_trait_accolade_policy": {
            "terminal_or_reuse_strength": "reuse_46_of_46",
            "token_integrity_risk": "low",
            "perspective_or_gender_risk": "low",
            "domain_context_risk": "medium",
            "expected_false_safe_risk": "medium",
            "expected_suggestion_yield": "small",
            "requires_parser": False,
            "requires_context_window": True,
            "resolver_complexity": "medium",
            "recommended_wave": 2,
            "rank": 7,
            "resolver_key": "effect_list_trait_accolade_resolver_dry_run",
            "rationale": "catalog reuse is complete, but trait/accolade domain labels need strict term guard",
        },
        "effect_list_gender_local_player_policy": {
            "terminal_or_reuse_strength": "reuse_52_of_55",
            "token_integrity_risk": "medium",
            "perspective_or_gender_risk": "high",
            "domain_context_risk": "medium",
            "expected_false_safe_risk": "high",
            "expected_suggestion_yield": "medium",
            "requires_parser": True,
            "requires_context_window": True,
            "resolver_complexity": "large",
            "recommended_wave": 3,
            "rank": 8,
            "resolver_key": "effect_list_gender_local_player_resolver_dry_run",
            "rationale": "reuse is high but local-player perspective errors are high-impact",
        },
        "select_cstring_requirement_policy": {
            "terminal_or_reuse_strength": "splitter_registered",
            "token_integrity_risk": "high",
            "perspective_or_gender_risk": "high",
            "domain_context_risk": "medium",
            "expected_false_safe_risk": "high",
            "expected_suggestion_yield": "medium",
            "requires_parser": True,
            "requires_context_window": True,
            "resolver_complexity": "large",
            "recommended_wave": 3,
            "rank": 9,
            "resolver_key": "select_cstring_requirement_resolver_dry_run",
            "rationale": "Select_CString must preserve branch structure and perspective before suggestions",
        },
        "gender_local_player_policy": {
            "terminal_or_reuse_strength": "cataloged_or_unregistered_in_current_registry",
            "token_integrity_risk": "medium",
            "perspective_or_gender_risk": "high",
            "domain_context_risk": "medium",
            "expected_false_safe_risk": "high",
            "expected_suggestion_yield": "medium",
            "requires_parser": True,
            "requires_context_window": True,
            "resolver_complexity": "large",
            "recommended_wave": 3,
            "rank": 10,
            "resolver_key": "gender_local_player_resolver_dry_run",
            "rationale": "important but should wait until branch/perspective guards are proven",
        },
    }
    rows: list[dict[str, Any]] = []
    for key in CANDIDATE_KEYS:
        meta = meta_by_key.get(key, {"registered": False, "notes": {}})
        row = {
            "record_type": "candidate_group",
            "policy_key": key,
            "registered": bool(meta.get("registered")),
            "candidate_segment_count": candidate_count(key, meta),
            **templates[key],
        }
        rows.append(row)
    return sorted(rows, key=lambda row: int(row["rank"]))


def write_outputs(*, args: argparse.Namespace, metrics: dict[str, int], candidates: list[dict[str, Any]]) -> tuple[Path, Path, Path]:
    txt_path, jsonl_path, plan_path = output_paths()
    first = candidates[0]
    plan = {
        "schema_version": 1,
        "source": "resolver_dry_run_strategy_v1",
        "segment_state_run_id": args.segment_state_run_id,
        "ledger_run_id": args.ledger_run_id,
        "coverage": {
            "pending_total": EXPECTED_PENDING_TOTAL,
            "routed_or_spec_coverage_projected": EXPECTED_ROUTED_COVERAGE,
            "segments_without_useful_spec": EXPECTED_WITHOUT_USEFUL_SPEC,
            "true_blocked_count": 0,
        },
        "registry_network": metrics,
        "candidate_ranking": candidates,
        "backlog_not_first_wave": BACKLOG,
        "metric_contract": METRIC_CONTRACT,
        "segment_record_contract": SEGMENT_RECORD_CONTRACT,
        "global_guards": GLOBAL_GUARDS,
        "first_resolver_recommended": first["resolver_key"],
        "first_resolver_source_policy": first["policy_key"],
        "first_resolver_sample_limit": 54,
        "next_prompt": "chat_exec_blocked_uncertain_token_integrity_debug_marker_resolver_dry_run_prompt.md",
        "future_apply_gate": {
            "false_safe_risk_count": 0,
            "blocked_by_token_integrity": 0,
            "requires_lifecycle_later": 0,
            "sample_validation": "100_percent",
            "human_review_required": True,
        },
    }
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps({
            "record_type": "summary",
            "first_resolver_recommended": first["resolver_key"],
            "first_resolver_source_policy": first["policy_key"],
            "first_resolver_sample_limit": 54,
            "production_full_recommended": False,
            "network_redesign_now": False,
            "network_data_only_after_first_wave": True,
            "next_prompt": plan["next_prompt"],
            **metrics,
        }, ensure_ascii=False, sort_keys=True) + "\n")
        for row in candidates:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        for key, value in BACKLOG.items():
            handle.write(json.dumps({"record_type": "backlog_remaining", "sublane": key, "count": value}, ensure_ascii=False, sort_keys=True) + "\n")
        handle.write(json.dumps({"record_type": "metric_contract", "fields": METRIC_CONTRACT}, ensure_ascii=False, sort_keys=True) + "\n")
        handle.write(json.dumps({"record_type": "global_guards", **GLOBAL_GUARDS}, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "resolver dry-run strategy",
        f"segment_state_run_id={args.segment_state_run_id}",
        f"ledger_run_id={args.ledger_run_id}",
        "",
        "registry/network:",
        f"- registered_agents: {metrics['registered_agents']}",
        f"- observed_agent_keys: {metrics['observed_agent_keys']}",
        f"- operational/dry_run/shadow: {metrics['operational_agents']}/{metrics['dry_run_agents']}/{metrics['shadow_agents']}",
        f"- terminal_guard_agents: {metrics['terminal_guard_agents']}",
        f"- splitter_agents: {metrics['splitter_agents']}",
        f"- Issue Network: {metrics['issue_network_agents']}",
        "",
        "ranking:",
        *[
            f"- wave {row['recommended_wave']} rank {row['rank']}: {row['policy_key']} "
            f"count={row['candidate_segment_count']} risk={row['expected_false_safe_risk']} complexity={row['resolver_complexity']}"
            for row in candidates
        ],
        "",
        "answers:",
        "1. Primeiro resolver dry-run recomendado: blocked_uncertain_token_integrity_debug_marker_resolver_dry_run.",
        "2. Motivo tecnico: bloco terminal 54/54, marcador #D ... #! preservavel, baixo risco semantico e sem necessidade de contexto amplo.",
        "3. Limite inicial de amostra: 54 segmentos, todos do bloco terminal.",
        "4. Gates que bloqueiam sugestao: quebra de token/dynamic/bracket/tag/variable, perda de #D ... #!, mudanca de Select_CString/ES/scope, contexto insuficiente, risco de falso seguro.",
        "5. Considerar apply futuro somente com false_safe_risk_count=0, blocked_by_token_integrity=0, requires_lifecycle_later=0, 100% da amostra validada e revisao humana aprovando.",
        "6. Producao full ainda nao: a arquitetura estabilizou, mas nenhum resolver dry-run provou ganho mensuravel com risco zero.",
        "7. Network: sem redesign; no maximo data-only depois da primeira onda de resolvers.",
        "",
        "required future resolver metrics:",
        *[f"- {field}" for field in METRIC_CONTRACT],
        "",
        "next_prompt=chat_exec_blocked_uncertain_token_integrity_debug_marker_resolver_dry_run_prompt.md",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, plan_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Design the first resolver dry-run strategy after architecture consolidation.")
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    parser.add_argument("--ledger-run-id", required=True, type=int)
    args = parser.parse_args()
    if args.segment_state_run_id != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit(f"segment_state_run_id guard failed: {args.segment_state_run_id}")
    if args.ledger_run_id != EXPECTED_LEDGER_RUN_ID:
        raise SystemExit(f"ledger_run_id guard failed: {args.ledger_run_id}")
    final_inventory = read_json(FINAL_ARCHITECTURE_INVENTORY)
    if final_inventory["summary"]["coverage_after_blocked_uncertain_projected"] != EXPECTED_ROUTED_COVERAGE:
        raise SystemExit("final architecture coverage guard failed")
    if final_inventory["summary"]["segments_without_useful_spec_projected"] != EXPECTED_WITHOUT_USEFUL_SPEC:
        raise SystemExit("final architecture remaining guard failed")
    with connect_readonly() as conn:
        metrics = registry_metrics(conn)
        candidates = build_candidate_plan(fetch_candidate_notes(conn))
    txt_path, jsonl_path, plan_path = write_outputs(args=args, metrics=metrics, candidates=candidates)
    print(f"txt: {txt_path}")
    print(f"jsonl: {jsonl_path}")
    print(f"plan: {plan_path}")
    print("first_resolver_recommended: blocked_uncertain_token_integrity_debug_marker_resolver_dry_run")
    print("next_prompt: chat_exec_blocked_uncertain_token_integrity_debug_marker_resolver_dry_run_prompt.md")


if __name__ == "__main__":
    main()
