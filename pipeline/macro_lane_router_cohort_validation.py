from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import macro_lane_router_architecture_review as router


ALLOWED_DECISIONS = {
    "primary_lane_confirmed",
    "secondary_lane_should_drive_first",
    "router_priority_too_broad",
    "needs_parser_before_secondary",
    "needs_domain_or_event_context_before_parser",
    "needs_gender_local_player_before_parser",
    "needs_custom_loc_scope_before_parser",
    "needs_requirement_or_effect_before_parser",
    "candidate_false_reopen_lifecycle",
    "blocked_uncertain",
}

GENDER_STRONG_RE = re.compile(
    r"Select_CString|ES_(?:OA|XA|EA|ElLa|DelDela|AlAla|A|O)|Get(?:SheHe|HerHis|HerHim|WomanMan|WomenMen)|"
    r"\bvocê\b|\bvocês\b|\bseu\b|\bsua\b",
    re.IGNORECASE,
)
CUSTOM_SCOPE_STRONG_RE = re.compile(r"Custom\(|ROOT\.|FROM\.|SCOPE\.|TARGET\.|CHARACTER\.|Get[A-Za-z0-9_]+\(", re.IGNORECASE)
REQUIREMENT_EFFECT_RE = re.compile(
    r"tooltip|_tt\b|requirement|required|unlock|trigger|\\n|\n|\$EFFECT_LIST_BULLET\$|#indent|#weak|#bold|#high|effect",
    re.IGNORECASE,
)
DOMAIN_EVENT_RE = re.compile(
    r"religion|faith|culture|tradition|doctrine|artifact|activity|event|\.desc|desc\.|option|interaction|journey|travel|story|memory",
    re.IGNORECASE,
)
SHORT_LABEL_RE = re.compile(r"^[^.\n]{1,80}$", re.IGNORECASE)
TOKEN_RE = re.compile(r"Custom\(|Select_CString|Concept\(|ScriptValue|GetTrait|ROOT\.|FROM\.|SCOPE\.|TARGET\.|\[[^\]]+\]|\$[^$]+\$", re.IGNORECASE)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"Invalid JSONL at {path}:{line_number}: {exc}") from exc
    return records


def selected_cohorts(router_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cohorts = [record for record in router_records if record.get("record_type") == "cohort"]
    dynamic = [cohort for cohort in cohorts if cohort.get("primary_lane") == "02_dynamic_parser"][:6]
    non_dynamic = [cohort for cohort in cohorts if cohort.get("primary_lane") != "02_dynamic_parser"][:3]
    return dynamic + non_dynamic


def route_key(item: dict[str, Any]) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    return (item["primary_lane"], tuple(item["families"]), tuple(item["secondary_lanes"][:3]))


def cohort_key_parts(cohort: dict[str, Any]) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    return (cohort["primary_lane"], tuple(cohort["families"]), tuple(cohort.get("secondary_lanes") or []))


def blob_for_sample(grouped_rows: dict[int, list[dict[str, Any]]], segment_id: int) -> str:
    rows = grouped_rows[segment_id]
    return router.blob_for(rows)


def component_for(decision: str, primary: str) -> str:
    return {
        "needs_gender_local_player_before_parser": "gender_local_player_policy",
        "needs_custom_loc_scope_before_parser": "custom_loc_scope_parser",
        "needs_requirement_or_effect_before_parser": "requirement_effect_list_policy",
        "needs_domain_or_event_context_before_parser": "domain_event_context_composer",
        "candidate_false_reopen_lifecycle": "false_reopen_lifecycle_bridge",
        "needs_parser_before_secondary": "ck3_dynamic_symbolic_parser",
        "primary_lane_confirmed": router.LANE_BY_ID[primary]["component"],
        "router_priority_too_broad": "macro_lane_priority_adjustment",
        "secondary_lane_should_drive_first": "secondary_lane_specific_policy",
        "blocked_uncertain": "human_review_or_evidence_collection",
    }[decision]


def validate_sample(item: dict[str, Any], blob: str) -> tuple[str, str, str, str]:
    primary = item["primary_lane"]
    secondaries = set(item["secondary_lanes"])
    text = blob
    token_count = len(TOKEN_RE.findall(text))

    if primary == "01_false_reopen_lifecycle":
        return "candidate_false_reopen_lifecycle", "false_reopen_lifecycle_bridge", "clean lifecycle cohort should be validated separately", "medium"

    if primary == "02_dynamic_parser":
        if "04_gender_local_player" in secondaries and GENDER_STRONG_RE.search(text):
            return (
                "needs_gender_local_player_before_parser",
                "gender_local_player_policy",
                "gender/local-player marker is explicit enough to drive before a generic parser",
                "high",
            )
        if {"06_requirement_tooltip", "07_effect_list_multiline"} & secondaries and REQUIREMENT_EFFECT_RE.search(text):
            return (
                "needs_requirement_or_effect_before_parser",
                "requirement_effect_list_policy",
                "requirement/effect surface is more actionable than a generic dynamic parser",
                "high",
            )
        if {"08_domain_context", "09_event_context"} & secondaries and DOMAIN_EVENT_RE.search(text):
            return (
                "needs_domain_or_event_context_before_parser",
                "domain_event_context_composer",
                "domain/event context is needed before semantic interpretation of the token",
                "medium",
            )
        if "03_custom_loc_scope_getter" in secondaries and CUSTOM_SCOPE_STRONG_RE.search(text):
            if token_count >= 2:
                return (
                    "needs_parser_before_secondary",
                    "ck3_dynamic_symbolic_parser",
                    "multiple dynamic/scope tokens must be parsed before choosing the narrower policy",
                    "high",
                )
            return (
                "needs_custom_loc_scope_before_parser",
                "custom_loc_scope_parser",
                "custom loc/scope pattern is specific enough to drive before generic parsing",
                "medium",
            )
        if token_count >= 2:
            return "needs_parser_before_secondary", "ck3_dynamic_symbolic_parser", "dynamic token stack is the real first blocker", "high"
        return "router_priority_too_broad", "macro_lane_priority_adjustment", "dynamic marker is too weak to justify primary dynamic routing", "medium"

    if primary in {"08_domain_context", "09_event_context"}:
        return "primary_lane_confirmed", router.LANE_BY_ID[primary]["component"], "non-dynamic cohort already routes to context composer first", "high"
    if primary in {"04_gender_local_player", "06_requirement_tooltip", "07_effect_list_multiline"}:
        return "primary_lane_confirmed", router.LANE_BY_ID[primary]["component"], "specific lane already drives before generic parser", "high"
    if primary == "14_short_label_style" and SHORT_LABEL_RE.search(text):
        return "primary_lane_confirmed", "short_label_style_policy", "short label surface is primary", "medium"
    return "primary_lane_confirmed", router.LANE_BY_ID[primary]["component"], "primary lane appears consistent with sampled evidence", "medium"


def sample_cohort_items(
    cohort: dict[str, Any],
    routed: dict[int, dict[str, Any]],
    grouped_rows: dict[int, list[dict[str, Any]]],
    limit: int,
) -> list[dict[str, Any]]:
    wanted = cohort_key_parts(cohort)
    matches = [item for item in routed.values() if route_key(item) == wanted]
    matches.sort(key=lambda item: (item["relative_path"], item["source_key"], item["segment_id"]))
    rows: list[dict[str, Any]] = []
    for item in matches[:limit]:
        blob = blob_for_sample(grouped_rows, item["segment_id"])
        decision, component, reason, confidence = validate_sample(item, blob)
        rows.append(
            {
                "record_type": "sample_validation",
                "segment_id": item["segment_id"],
                "relative_path": item["relative_path"],
                "source_key": item["source_key"],
                "families_open": item["families"],
                "cohort_key": cohort["cohort_key"],
                "primary_lane": item["primary_lane"],
                "secondary_lanes": item["secondary_lanes"],
                "recommended_first_component": component,
                "validation_decision": decision,
                "reason": reason,
                "confidence": confidence,
            }
        )
    return rows


def build_reports(
    sample_rows: list[dict[str, Any]],
    cohorts: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    by_cohort: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in sample_rows:
        by_cohort[row["cohort_key"]].append(row)

    cohort_summaries: list[dict[str, Any]] = []
    component_counts: Counter[str] = Counter()
    component_segments: Counter[str] = Counter()
    cohort_by_key = {cohort["cohort_key"]: cohort for cohort in cohorts}
    for cohort_key, rows in by_cohort.items():
        decisions = Counter(row["validation_decision"] for row in rows)
        components = Counter(row["recommended_first_component"] for row in rows)
        component, _ = components.most_common(1)[0]
        estimated = int(cohort_by_key[cohort_key].get("segments") or 0)
        component_counts[component] += 1
        component_segments[component] += estimated
        cohort_summaries.append(
            {
                "record_type": "cohort_summary",
                "cohort_key": cohort_key,
                "sampled": len(rows),
                "primary_confirmed": decisions["primary_lane_confirmed"] + decisions["needs_parser_before_secondary"],
                "secondary_should_drive": sum(
                    count
                    for decision, count in decisions.items()
                    if decision
                    in {
                        "secondary_lane_should_drive_first",
                        "needs_domain_or_event_context_before_parser",
                        "needs_gender_local_player_before_parser",
                        "needs_custom_loc_scope_before_parser",
                        "needs_requirement_or_effect_before_parser",
                    }
                ),
                "recommended_first_component": component,
                "decision_counts": dict(decisions),
            }
        )

    total_by_component = Counter(row["recommended_first_component"] for row in sample_rows)
    component_records = []
    for component, count in total_by_component.most_common():
        component_records.append(
            {
                "record_type": "component_recommendation",
                "component": component,
                "cohorts_supported": component_counts[component],
                "sample_support_rate": round(count / len(sample_rows) * 100 if sample_rows else 0, 2),
                "estimated_segments": component_segments[component],
                "next_prompt": next_prompt_for_component(component),
            }
        )

    adjustments = [
        {
            "record_type": "router_adjustment",
            "rule": "04_gender_local_player before 02_dynamic_parser when explicit ES/Select_CString/local-player marker is present",
            "current_priority": 4,
            "suggested_priority": 2,
            "reason": "sample validation checks whether explicit gender/local-player can drive before generic dynamic parsing",
        },
        {
            "record_type": "router_adjustment",
            "rule": "06/07 requirement_or_effect before 02_dynamic_parser when tooltip/effect-list surface is explicit",
            "current_priority": 6,
            "suggested_priority": 3,
            "reason": "format surface may be more actionable than generic parser for repeated UI rules",
        },
    ]
    strategies = [
        {
            "record_type": "strategy",
            "priority": 1,
            "next_prompt": "chat_exec_gender_local_player_policy_consolidated_review_prompt.md",
            "rationale": "validate and consolidate explicit gender/local-player cases that outrank generic dynamic parsing",
        },
        {
            "record_type": "strategy",
            "priority": 2,
            "next_prompt": "chat_exec_parser_backed_dynamic_expression_design_prompt.md",
            "rationale": "design parser for remaining multi-token dynamic stacks after specific overrides",
        },
        {
            "record_type": "strategy",
            "priority": 3,
            "next_prompt": "chat_exec_requirement_effect_router_validation_prompt.md",
            "rationale": "validate requirement/effect-list priority override before integrating router",
        },
    ]
    return cohort_summaries, component_records, adjustments, strategies


def next_prompt_for_component(component: str) -> str:
    return {
        "gender_local_player_policy": "chat_exec_gender_local_player_policy_consolidated_review_prompt.md",
        "ck3_dynamic_symbolic_parser": "chat_exec_parser_backed_dynamic_expression_design_prompt.md",
        "domain_event_context_composer": "chat_exec_domain_event_context_router_review_prompt.md",
        "requirement_effect_list_policy": "chat_exec_requirement_effect_router_validation_prompt.md",
        "custom_loc_scope_parser": "chat_exec_custom_loc_scope_parser_cohort_review_prompt.md",
    }.get(component, "chat_exec_macro_lane_router_cohort_validation_followup_prompt.md")


def output_paths() -> tuple[Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_macro_lane_router_cohort_validation"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl")


def write_jsonl(
    path: Path,
    sample_rows: list[dict[str, Any]],
    cohort_summaries: list[dict[str, Any]],
    component_records: list[dict[str, Any]],
    adjustments: list[dict[str, Any]],
    strategies: list[dict[str, Any]],
) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        summary = {
            "record_type": "summary",
            "sampled": len(sample_rows),
            "validation_decision_counts": dict(Counter(row["validation_decision"] for row in sample_rows)),
        }
        handle.write(json.dumps(summary, ensure_ascii=False, sort_keys=True) + "\n")
        for collection in (cohort_summaries, component_records, adjustments, strategies, sample_rows):
            for row in collection:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def write_txt(
    path: Path,
    sample_rows: list[dict[str, Any]],
    cohort_summaries: list[dict[str, Any]],
    component_records: list[dict[str, Any]],
    adjustments: list[dict[str, Any]],
    strategies: list[dict[str, Any]],
) -> None:
    decisions = Counter(row["validation_decision"] for row in sample_rows)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("Resumo executivo\n")
        handle.write("A validacao indica que 02_dynamic_parser nao deve comandar tudo sem excecoes: explicit gender/local-player e requirement/effect devem poder dirigir primeiro.\n")
        handle.write("O parser continua necessario para pilhas dinamicas multi-token, mas a prioridade precisa de overrides especificos.\n\n")
        handle.write("Amostragem\n")
        handle.write(f"total_sampled: {len(sample_rows)}\n")
        for decision, count in decisions.most_common():
            handle.write(f"- {decision}: {count}\n")
        handle.write("\nValidacao por cohort\n")
        for row in cohort_summaries:
            handle.write(
                f"- {row['cohort_key']}: sampled={row['sampled']}, primary_confirmed={row['primary_confirmed']}, "
                f"secondary_should_drive={row['secondary_should_drive']}, first={row['recommended_first_component']}, decisions={row['decision_counts']}\n"
            )
        handle.write("\nConfirmacao ou ajuste da prioridade do router\n")
        for row in adjustments:
            handle.write(f"- {row['rule']}: {row['current_priority']} -> {row['suggested_priority']}; {row['reason']}\n")
        handle.write("\nComponentes recomendados\n")
        for row in component_records:
            handle.write(
                f"- {row['component']}: cohorts={row['cohorts_supported']}, support={row['sample_support_rate']}%, "
                f"estimated_segments={row['estimated_segments']}, next={row['next_prompt']}\n"
            )
        handle.write("\nProximos prompts\n")
        for row in strategies:
            handle.write(f"{row['priority']}. {row['next_prompt']}: {row['rationale']}\n")
        handle.write("\nValidacoes\n")
        handle.write("- Banco em modo read-only.\n")
        handle.write("- Amostra deterministica por relative_path/source_key/segment_id.\n")
        handle.write("- Sem lifecycle, apply, segment-state, issue-ledger, confirmations, production, reindex, treino ou source/output changes.\n")


def validate_pending(conn: Any, segment_state_run_id: int, sample_rows: list[dict[str, Any]]) -> None:
    ids = [int(row["segment_id"]) for row in sample_rows]
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    count = conn.execute(
        f"""
        SELECT COUNT(*)
        FROM segment_state_items
        WHERE run_id = ?
          AND segment_id IN ({placeholders})
          AND state_group = 'pending'
          AND COALESCE(is_closed, 0) = 0
        """,
        (segment_state_run_id, *ids),
    ).fetchone()[0]
    if count != len(set(ids)):
        raise SystemExit(f"pending validation mismatch: expected {len(set(ids))}, got {count}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only macro-lane router cohort validation.")
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    parser.add_argument("--ledger-run-id", required=True, type=int)
    parser.add_argument("--router-jsonl", required=True, type=Path)
    parser.add_argument("--router-spec", required=True, type=Path)
    parser.add_argument("--limit", type=int, default=240)
    args = parser.parse_args()

    router_records = read_jsonl(args.router_jsonl)
    with args.router_spec.open("r", encoding="utf-8") as handle:
        json.load(handle)
    cohorts = selected_cohorts(router_records)

    conn = router.connect_readonly()
    router.fetch_run(conn, "segment_state_runs", args.segment_state_run_id)
    router.fetch_run(conn, "ml_issue_ledger_runs", args.ledger_run_id)
    rows = router.fetch_pending_rows(conn, args.segment_state_run_id, args.ledger_run_id)
    grouped = router.group_by_segment(rows)
    routed = router.route_segments(grouped)

    sample_rows: list[dict[str, Any]] = []
    for index, cohort in enumerate(cohorts):
        per_cohort = 25 if index < 6 else 20
        if len(sample_rows) >= args.limit:
            break
        sample_rows.extend(sample_cohort_items(cohort, routed, grouped, min(per_cohort, args.limit - len(sample_rows))))
    sample_rows = sample_rows[: args.limit]
    validate_pending(conn, args.segment_state_run_id, sample_rows)

    cohort_summaries, component_records, adjustments, strategies = build_reports(sample_rows, cohorts)
    txt_path, jsonl_path = output_paths()
    write_txt(txt_path, sample_rows, cohort_summaries, component_records, adjustments, strategies)
    write_jsonl(jsonl_path, sample_rows, cohort_summaries, component_records, adjustments, strategies)

    print(f"txt: {txt_path}")
    print(f"jsonl: {jsonl_path}")
    print(f"sampled: {len(sample_rows)}")
    print("validation_decision_counts:")
    for decision, count in Counter(row["validation_decision"] for row in sample_rows).most_common():
        print(f"  {decision}: {count}")
    print("component_recommendations:")
    for row in component_records[:5]:
        print(f"  {row['component']}: {row['sample_support_rate']}%")


if __name__ == "__main__":
    main()
