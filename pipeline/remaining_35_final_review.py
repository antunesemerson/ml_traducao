from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


REVIEW_KEY = "remaining_35_final_review"
EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76
EXPECTED_TOTAL = 35

BASE_BLOCKED_JSONL = Path("reports/20260623_123342_502844_blocked_uncertain_review.jsonl")
RELIGION_CULTURE_JSONL = Path("reports/20260623_124333_102477_blocked_uncertain_religion_culture_review.jsonl")
FAITH_DOCTRINE_JSONL = Path("reports/20260623_125447_651827_blocked_uncertain_religion_culture_faith_doctrine_review.jsonl")
DOCTRINE_TENET_JSONL = Path("reports/20260623_130101_323420_blocked_uncertain_religion_culture_faith_doctrine_doctrine_tenet_review.jsonl")
DOCTRINE_GROUP_JSONL = Path("reports/20260623_130814_897985_blocked_uncertain_religion_culture_faith_doctrine_doctrine_tenet_doctrine_group_review.jsonl")
FAITH_NAME_JSONL = Path("reports/20260623_133641_101414_blocked_uncertain_religion_culture_faith_doctrine_faith_name_review.jsonl")

REQUIRED_INPUTS = [
    "reports/20260623_164747_499852_global_final_architecture_before_resolution_diagnostic.jsonl",
    "reports/20260623_164747_499852_global_final_architecture_before_resolution_diagnostic_inventory.json",
    "reports/20260623_153737_123635_global_final_blocked_uncertain_architecture_diagnostic.jsonl",
    "reports/20260623_164015_357572_blocked_uncertain_policy_catalog_registry_apply.jsonl",
    "reports/20260623_220129_729915_semantic_short_label_autofix_candidate_discovery_summary.json",
]

DECISION_MAP = {
    "needs_blocked_religion_culture_tenet_policy": "remaining_religion_culture_tenet_policy",
    "needs_doctrine_group_name_policy": "remaining_doctrine_group_name_policy",
    "needs_doctrine_tenet_short_label_policy": "remaining_doctrine_tenet_short_label_policy",
    "needs_faith_doctrine_gender_perspective_policy": "remaining_faith_doctrine_gender_perspective_policy",
    "needs_faith_doctrine_short_label_policy": "remaining_faith_doctrine_short_label_policy",
    "blocked_religion_culture_terminal_guard": "remaining_religion_culture_terminal_guard",
    "blocked_religion_culture_reuse_domain_context_religion_holy_site_policy": "remaining_religion_culture_reuse_domain_context",
    "needs_blocked_language_residual_policy": "remaining_language_residual_policy",
    "needs_blocked_name_title_culture_policy": "remaining_name_title_culture_policy",
}


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_remaining_35_final_review"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), reports_dir / f"{base.name}_summary.json"


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    full_path = db.project_path(str(path))
    rows: list[dict[str, Any]] = []
    with full_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"Invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def sample_rows(path: Path) -> list[dict[str, Any]]:
    rows = [row for row in read_jsonl(path) if row.get("record_type") == "sample_review"]
    ids = [int(row["segment_id"]) for row in rows]
    if len(ids) != len(set(ids)):
        raise SystemExit(f"duplicate segment_id in {path}")
    return rows


def summary_row(path: Path, label: str) -> dict[str, Any]:
    summaries = [row for row in read_jsonl(path) if row.get("record_type") == "summary"]
    if len(summaries) != 1:
        raise SystemExit(f"expected exactly one summary in {label}, got {len(summaries)}")
    return summaries[0]


def validate_required_inputs() -> None:
    for rel_path in REQUIRED_INPUTS:
        path = db.project_path(rel_path)
        if not path.exists():
            raise SystemExit(f"missing required artifact: {path}")
    discovery = read_json(db.project_path("reports/20260623_220129_729915_semantic_short_label_autofix_candidate_discovery_summary.json"))
    if int(discovery.get("candidate_count") or 0) != 8:
        raise SystemExit("candidate discovery summary guard failed")
    registry = summary_row(Path("reports/20260623_164015_357572_blocked_uncertain_policy_catalog_registry_apply.jsonl"), "blocked parent registry")
    if int(registry.get("blocked_uncertain_remaining_without_useful_spec") or 0) != EXPECTED_TOTAL:
        raise SystemExit("blocked registry remaining guard failed")
    if int(registry.get("true_blocked_count") or 0) != 0:
        raise SystemExit("blocked registry true_blocked guard failed")


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def terminal_sets() -> dict[str, set[int]]:
    doctrine = {
        int(row["segment_id"])
        for row in sample_rows(DOCTRINE_GROUP_JSONL)
        if row.get("doctrine_group_decision") == "doctrine_group_terminal_guard_with_domain_guard"
    }
    faith = {
        int(row["segment_id"])
        for row in sample_rows(FAITH_NAME_JSONL)
        if row.get("faith_name_decision") == "faith_name_terminal_guard_with_domain_guard"
    }
    if len(doctrine) != 15 or len(faith) != 17:
        raise SystemExit(f"terminal set guard failed: doctrine={len(doctrine)} faith={len(faith)}")
    return {"doctrine": doctrine, "faith": faith}


def by_id(rows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {int(row["segment_id"]): row for row in rows}


def choose_source_row(segment_id: int, maps: list[dict[int, dict[str, Any]]]) -> dict[str, Any]:
    for mapping in maps:
        if segment_id in mapping:
            return mapping[segment_id]
    raise SystemExit(f"missing source row for {segment_id}")


def reconstruct_remaining() -> list[dict[str, Any]]:
    base = sample_rows(BASE_BLOCKED_JSONL)
    religion = sample_rows(RELIGION_CULTURE_JSONL)
    faith_doctrine = sample_rows(FAITH_DOCTRINE_JSONL)
    doctrine_tenet = sample_rows(DOCTRINE_TENET_JSONL)
    doctrine_group = sample_rows(DOCTRINE_GROUP_JSONL)

    if len(base) != 144 or len(religion) != 60:
        raise SystemExit("base/religion review count guard failed")
    terminals = terminal_sets()
    base_by_id = by_id(base)
    religion_by_id = by_id(religion)
    faith_by_id = by_id(faith_doctrine)
    tenet_by_id = by_id(doctrine_tenet)
    group_by_id = by_id(doctrine_group)

    records: list[dict[str, Any]] = []
    for row in religion:
        sid = int(row["segment_id"])
        if sid in terminals["doctrine"] or sid in terminals["faith"]:
            continue
        decision = str(row.get("religion_culture_decision") or "")
        source = row
        if sid in group_by_id and group_by_id[sid].get("doctrine_group_decision") == "needs_doctrine_group_name_policy":
            decision = "needs_doctrine_group_name_policy"
            source = group_by_id[sid]
        elif sid in tenet_by_id and tenet_by_id[sid].get("doctrine_tenet_decision") == "needs_doctrine_tenet_short_label_policy":
            decision = "needs_doctrine_tenet_short_label_policy"
            source = tenet_by_id[sid]
        elif sid in faith_by_id:
            faith_decision = str(faith_by_id[sid].get("faith_doctrine_decision") or "")
            if faith_decision in {"needs_faith_doctrine_gender_perspective_policy", "needs_faith_doctrine_short_label_policy"}:
                decision = faith_decision
                source = faith_by_id[sid]
        bucket = DECISION_MAP.get(decision, "remaining_true_unknown")
        records.append(record_from_source(sid, bucket, source, decision))

    for row in base:
        decision = str(row.get("blocked_decision") or "")
        if decision in {"needs_blocked_language_residual_policy", "needs_blocked_name_title_culture_policy"}:
            records.append(record_from_source(int(row["segment_id"]), DECISION_MAP[decision], row, decision))

    ids = [int(row["segment_id"]) for row in records]
    if len(ids) != len(set(ids)):
        raise SystemExit("duplicate remaining segment_id")
    return sorted(records, key=lambda row: (row["decision"], row["segment_id"]))


def record_from_source(segment_id: int, bucket: str, source: dict[str, Any], source_decision: str) -> dict[str, Any]:
    requires_new_policy = bucket == "remaining_religion_culture_tenet_policy"
    existing_policy_reuse = ""
    if bucket == "remaining_religion_culture_reuse_domain_context":
        existing_policy_reuse = "domain_context_religion_holy_site_policy"
    elif bucket == "remaining_religion_culture_terminal_guard":
        existing_policy_reuse = "blocked_uncertain_religion_culture_policy_terminal_guard"
    return {
        "segment_id": segment_id,
        "review_key": REVIEW_KEY,
        "remaining_bucket": bucket,
        "decision": bucket,
        "open_families": list(source.get("families_open") or []),
        "existing_policy_reuse": existing_policy_reuse,
        "original_text": str(source.get("old_text") or ""),
        "current_output_text": str(source.get("output_text") or source.get("confirmed_text") or ""),
        "requires_new_policy": requires_new_policy,
        "register_component_now": False,
        "candidate_for_resolver": False,
        "requires_apply_later": False,
        "requires_lifecycle_later": False,
        "false_safe_risk": False,
        "notes": f"source_decision={source_decision}; catalog as final leftover, no apply",
    }


def validate_pending(conn: sqlite3.Connection, records: list[dict[str, Any]], run_id: int) -> None:
    ids = [int(row["segment_id"]) for row in records]
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT segment_id, state_group, is_closed, needs_output_apply, confirmed_matches_output
        FROM segment_state_items
        WHERE run_id = ? AND segment_id IN ({placeholders})
        """,
        (run_id, *ids),
    ).fetchall()
    if len(rows) != len(ids):
        raise SystemExit(f"pending validation missing rows: {len(ids) - len(rows)}")
    bad = [
        dict(row)
        for row in rows
        if row["state_group"] != "pending"
        or int(row["is_closed"] or 0) != 0
        or int(row["needs_output_apply"] or 0) != 0
        or int(row["confirmed_matches_output"] or 0) != 1
    ]
    if bad:
        raise SystemExit(f"pending validation failed: {bad[:3]}")


def build_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    decisions = Counter(row["decision"] for row in records)
    summary = {
        "schema_version": 1,
        "source": "remaining_35_final_review_v1",
        "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
        "ledger_run_id": EXPECTED_LEDGER_RUN_ID,
        "total_reviewed": len(records),
        "remaining_religion_culture_tenet_policy": decisions.get("remaining_religion_culture_tenet_policy", 0),
        "remaining_doctrine_group_name_policy": decisions.get("remaining_doctrine_group_name_policy", 0),
        "remaining_doctrine_tenet_short_label_policy": decisions.get("remaining_doctrine_tenet_short_label_policy", 0),
        "remaining_faith_doctrine_gender_perspective_policy": decisions.get("remaining_faith_doctrine_gender_perspective_policy", 0),
        "remaining_faith_doctrine_short_label_policy": decisions.get("remaining_faith_doctrine_short_label_policy", 0),
        "remaining_religion_culture_terminal_guard": decisions.get("remaining_religion_culture_terminal_guard", 0),
        "remaining_religion_culture_reuse_domain_context": decisions.get("remaining_religion_culture_reuse_domain_context", 0),
        "remaining_language_residual_policy": decisions.get("remaining_language_residual_policy", 0),
        "remaining_name_title_culture_policy": decisions.get("remaining_name_title_culture_policy", 0),
        "remaining_already_covered_by_existing_policy": decisions.get("remaining_already_covered_by_existing_policy", 0),
        "remaining_true_unknown": decisions.get("remaining_true_unknown", 0),
        "requires_new_policy_count": sum(1 for row in records if row["requires_new_policy"]),
        "register_component_now_count": sum(1 for row in records if row["register_component_now"]),
        "candidate_for_resolver_count": sum(1 for row in records if row["candidate_for_resolver"]),
        "requires_apply_later_count": sum(1 for row in records if row["requires_apply_later"]),
        "requires_lifecycle_later_count": sum(1 for row in records if row["requires_lifecycle_later"]),
        "false_safe_risk_count": sum(1 for row in records if row["false_safe_risk"]),
        "decision_counts": dict(sorted(decisions.items())),
        "routing_architecture_closed": decisions.get("remaining_true_unknown", 0) == 0,
        "next_prompt": "chat_exec_remaining_religion_culture_tenet_policy_review_prompt.md"
        if decisions.get("remaining_religion_culture_tenet_policy", 0) >= 15
        else "chat_exec_resolution_phase_status_and_next_strategy_prompt.md",
    }
    return summary


def write_outputs(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    txt_path, jsonl_path, summary_path = output_paths()
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    metric_keys = [
        "total_reviewed",
        "remaining_religion_culture_tenet_policy",
        "remaining_doctrine_group_name_policy",
        "remaining_doctrine_tenet_short_label_policy",
        "remaining_faith_doctrine_gender_perspective_policy",
        "remaining_faith_doctrine_short_label_policy",
        "remaining_religion_culture_terminal_guard",
        "remaining_religion_culture_reuse_domain_context",
        "remaining_language_residual_policy",
        "remaining_name_title_culture_policy",
        "remaining_already_covered_by_existing_policy",
        "remaining_true_unknown",
        "requires_new_policy_count",
        "register_component_now_count",
        "candidate_for_resolver_count",
        "requires_apply_later_count",
        "requires_lifecycle_later_count",
        "false_safe_risk_count",
    ]
    lines = [
        "remaining 35 final review",
        f"segment_state_run_id={summary['segment_state_run_id']}",
        f"ledger_run_id={summary['ledger_run_id']}",
        "",
        *[f"{key}={summary[key]}" for key in metric_keys],
        "",
        "analysis:",
        "1. Os 35 sao sem spec util no sentido arquitetural: sim, mas todos estao explicados por leftover conhecido.",
        "2. Novo componente registrado agora: nao; o bloco dominante justifica micro-policy read-only estreita, nao registro imediato.",
        "3. Resolver dry-run agora: nao; primeiro revisar micro-policy tenet.",
        f"4. True unknown: {summary['remaining_true_unknown']}.",
        "5. Para declarar roteamento arquitetural fechado: revisar/catalogar tenet como micro-policy read-only e preservar os demais leftovers pequenos.",
        "6. Producao full agora: nao; ainda faltam candidatos de alteracao auditados.",
        "7. Network agora: sem redesign; data-only futuro pode expor 35 remaining.",
        "",
        f"routing_architecture_closed={str(summary['routing_architecture_closed']).lower()}",
        f"next_prompt={summary['next_prompt']}",
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
    if args.segment_state_run_id != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("segment_state_run_id argument guard failed")
    if args.ledger_run_id != EXPECTED_LEDGER_RUN_ID:
        raise SystemExit("ledger_run_id argument guard failed")
    validate_required_inputs()
    records = reconstruct_remaining()
    if len(records) != EXPECTED_TOTAL:
        raise SystemExit(f"remaining total guard failed: {len(records)}")
    with connect_readonly() as conn:
        validate_pending(conn, records, args.segment_state_run_id)
    summary = build_summary(records)
    if summary["total_reviewed"] != EXPECTED_TOTAL:
        raise SystemExit("total_reviewed summary guard failed")
    if summary["requires_apply_later_count"] or summary["requires_lifecycle_later_count"] or summary["false_safe_risk_count"]:
        raise SystemExit("zero-risk summary guard failed")
    txt_path, jsonl_path, summary_path = write_outputs(records, summary)
    print(f"txt: {txt_path}")
    print(f"jsonl: {jsonl_path}")
    print(f"summary: {summary_path}")
    for key in [
        "total_reviewed",
        "remaining_religion_culture_tenet_policy",
        "remaining_doctrine_group_name_policy",
        "remaining_doctrine_tenet_short_label_policy",
        "remaining_language_residual_policy",
        "remaining_name_title_culture_policy",
        "remaining_true_unknown",
        "requires_new_policy_count",
        "requires_apply_later_count",
        "requires_lifecycle_later_count",
        "false_safe_risk_count",
        "next_prompt",
    ]:
        print(f"{key}: {summary[key]}")


if __name__ == "__main__":
    main()
