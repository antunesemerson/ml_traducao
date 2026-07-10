from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import macro_lane_router_architecture_review as router


NODE_PATTERNS: dict[str, re.Pattern[str]] = {
    "formatting_tag": re.compile(r"#[A-Za-z0-9_:.{};,|]+|#!"),
    "variable": re.compile(r"\$[^$]+\$"),
    "icon": re.compile(r"@[A-Za-z0-9_]+!"),
    "bracket_expression": re.compile(r"\[[^\]]+\]"),
    "concept_link": re.compile(r"\[[A-Za-z0-9_]+\|[^\]]+\]|\[Concept\(|Concept\("),
    "custom_loc": re.compile(r"Custom\(|CustomLoc|\.Custom\(", re.I),
    "select_cstring": re.compile(r"Select_CString|SelectLocalization", re.I),
    "es_helper": re.compile(r"ES_(?:OA|XA|EA|ElLa|DelDela|AlAla|A|O)", re.I),
    "scope_getter": re.compile(r"ROOT\.|FROM\.|SCOPE\.|TARGET\.|CHARACTER\.|GetPlayer|GetLocalPlayer|Get[A-Za-z0-9_]+\(", re.I),
    "script_value": re.compile(r"ScriptValue|GetScriptValue|MakeScope|Localize\(", re.I),
    "trait_getter": re.compile(r"GetTrait|GetModifier|GetName|GetFirstName|GetHouse|GetDynasty|GetTitleAsName", re.I),
    "effect_list": re.compile(r"\n|\\n|\$EFFECT_LIST_BULLET\$|#indent|gain|loss|can_|cannot|unlock|requirement", re.I),
}

UNKNOWN_DYNAMIC_RE = re.compile(r"\[[^\]]*$|(?<!\$)\$[^$]*$|Select_[A-Za-z0-9_]+|Custom2|LocalPlayerString|PlayerString|Var\(", re.I)
GENDER_OVERRIDE_RE = re.compile(r"Select_CString|ES_(?:OA|XA|EA|ElLa|DelDela|AlAla|A|O)|Get(?:SheHe|HerHis|HerHim|WomanMan|WomenMen)|IsLocalPlayer|LocalPlayer", re.I)
REQ_EFFECT_OVERRIDE_RE = re.compile(r"tooltip|_tt\b|requirement|required|unlock|trigger|\n|\\n|\$EFFECT_LIST_BULLET\$|#indent|effect", re.I)
DOMAIN_RE = re.compile(r"religion|faith|culture|artifact|activity|title|law|government|nickname|dynasty|house", re.I)
EVENT_RE = re.compile(r"event|\.desc|desc\.|option|toast|dialogue|story|interaction|journey|travel|memory", re.I)
RESIDUAL_RE = re.compile(r"\b(the|your|you|their|has|will|cannot|consiguio|consiguió|sentisteis|sintieron)\b|NÃ|Ãƒ|Â", re.I)


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


def parse_nodes(text: str) -> tuple[list[str], list[str]]:
    nodes = [node for node, pattern in NODE_PATTERNS.items() if pattern.search(text)]
    unknowns = []
    for match in UNKNOWN_DYNAMIC_RE.finditer(text):
        unknowns.append(match.group(0)[:80])
    if not nodes and re.search(r"\[[^\]]+\]|\$[^$]+\$|Get[A-Za-z0-9_]+", text):
        nodes.append("unknown_dynamic")
    return sorted(set(nodes)), sorted(set(unknowns))


def text_blob(item: dict[str, Any], grouped: dict[int, list[dict[str, Any]]]) -> tuple[str, str, str, str]:
    row = grouped[int(item["segment_id"])][0]
    old_text = str(row.get("old_text") or "")
    confirmed_text = str(row.get("confirmed_text") or "")
    output_text = str(row.get("output_text") or "")
    blob = " ".join([item["relative_path"], item["source_key"], old_text, confirmed_text, output_text])
    return old_text, confirmed_text, output_text, blob


def sample_groups(routed: dict[int, dict[str, Any]], grouped: dict[int, list[dict[str, Any]]], limit: int) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in routed.values():
        _, _, _, blob = text_blob(item, grouped)
        primary = item["primary_lane"]
        secondaries = set(item["secondary_lanes"])
        if primary == "02_dynamic_parser" and not GENDER_OVERRIDE_RE.search(blob) and not REQ_EFFECT_OVERRIDE_RE.search(blob):
            bucket = "dynamic_parser_primary_after_overrides"
        elif "03_custom_loc_scope_getter" in secondaries:
            bucket = "custom_loc_scope_getter_secondary"
        elif "concept_link" in parse_nodes(blob)[0]:
            bucket = "concept_expression"
        elif re.search(r"ScriptValue|GetScriptValue|GetTrait|GetModifier", blob, re.I):
            bucket = "script_value_or_gettrait"
        elif "05_actor_target_recipient" in secondaries:
            bucket = "scope_getter_actor_target"
        elif "07_effect_list_multiline" in secondaries:
            bucket = "effect_list_dynamic_remaining"
        elif parse_nodes(blob)[1]:
            bucket = "unparsed_or_uncertain"
        else:
            continue
        buckets[bucket].append(item)

    quotas = [
        ("dynamic_parser_primary_after_overrides", 160),
        ("custom_loc_scope_getter_secondary", 60),
        ("concept_expression", 40),
        ("script_value_or_gettrait", 30),
        ("scope_getter_actor_target", 30),
        ("effect_list_dynamic_remaining", 20),
        ("unparsed_or_uncertain", 20),
    ]
    selected: list[dict[str, Any]] = []
    seen: set[int] = set()
    for bucket, quota in quotas:
        items = buckets[bucket]
        items.sort(key=lambda row: (row["relative_path"], row["source_key"], row["segment_id"]))
        for item in items:
            if len(selected) >= limit or quota <= 0:
                break
            if int(item["segment_id"]) in seen:
                continue
            payload = dict(item)
            payload["sample_bucket"] = bucket
            selected.append(payload)
            seen.add(int(item["segment_id"]))
            quota -= 1
    return selected


def classify(item: dict[str, Any], nodes: list[str], unknowns: list[str], blob: str) -> tuple[str, str, str, str]:
    if GENDER_OVERRIDE_RE.search(blob):
        return "parser_needs_gender_override_first", "gender_local_player_policy", "high", "explicit gender/local-player marker should route before generic parser"
    if REQ_EFFECT_OVERRIDE_RE.search(blob):
        return "parser_needs_requirement_effect_override_first", "requirement_effect_list_policy", "high", "requirement/effect-list surface should route before generic parser"
    if unknowns:
        return "parser_unparsed_unknown_dynamic", "ck3_dynamic_parser_unknown_queue", "low", "dynamic-looking pattern is not covered by current node set"
    if "custom_loc" in nodes:
        return "parser_needs_custom_loc_scope_stage", "custom_loc_scope_parser", "high", "custom loc/scope AST node is present"
    if "concept_link" in nodes:
        return "parser_needs_concept_expression_stage", "concept_expression_policy", "high", "concept expression AST node is present"
    if "script_value" in nodes:
        return "parser_needs_script_value_stage", "script_value_policy", "high", "script value AST node is present"
    if "scope_getter" in nodes:
        return "parser_needs_scope_getter_stage", "scope_getter_policy", "high", "scope/getter AST node is present"
    if "trait_getter" in nodes:
        return "parser_needs_trait_getter_stage", "trait_getter_policy", "high", "trait/name/modifier getter AST node is present"
    if "effect_list" in nodes:
        return "parser_needs_effect_list_stage", "effect_list_multiline_policy", "medium", "effect-list node remains after overrides"
    if DOMAIN_RE.search(blob):
        return "parser_needs_domain_context_after_parse", "domain_context_composer", "medium", "domain context remains after token parsing"
    if EVENT_RE.search(blob):
        return "parser_needs_event_context_after_parse", "event_context_composer", "medium", "event context remains after token parsing"
    if RESIDUAL_RE.search(blob):
        return "parser_needs_residual_repair_after_parse", "residual_dependency_filtered_repair", "medium", "residual should wait until parse/context is resolved"
    if nodes:
        return "parser_coverage_clean", "ck3_dynamic_symbolic_parser", "high", "all detected dynamic nodes are covered by current prototype"
    return "parser_blocked_uncertain", "human_review_or_evidence_collection", "low", "no parser-relevant node detected in sampled dynamic cohort"


def build_spec(run_id: int, ledger_run_id: int, node_counts: Counter[str], decisions: Counter[str]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "created_for": "read_only_parser_design",
        "segment_state_run_id": run_id,
        "ledger_run_id": ledger_run_id,
        "parser_id": "ck3_dynamic_expression_parser",
        "overrides_before_parser": ["gender_local_player_policy", "requirement_effect_list_policy"],
        "node_types": [
            "text",
            "formatting_tag",
            "variable",
            "icon",
            "bracket_expression",
            "concept_link",
            "custom_loc",
            "select_cstring",
            "es_helper",
            "scope_getter",
            "script_value",
            "trait_getter",
            "effect_list",
            "unknown_dynamic",
        ],
        "parse_order": [
            "split formatting tags, variables, icons",
            "extract bracket expressions",
            "classify concept links and CustomLoc",
            "classify Select_CString and ES helpers",
            "classify scope/getters, trait getters, script values",
            "detect requirement/effect-list surfaces",
            "emit unknown_dynamic for unmatched CK3-like expressions",
        ],
        "handoff_lanes": [
            "custom_loc_scope_parser",
            "concept_expression_policy",
            "script_value_policy",
            "scope_getter_policy",
            "trait_getter_policy",
            "domain_context_composer",
            "event_context_composer",
            "residual_dependency_filtered_repair",
        ],
        "blocked_conditions": [
            "unbalanced brackets or variables",
            "unknown dynamic pattern",
            "gender/local-player marker not routed first",
            "requirement/effect-list surface not routed first",
        ],
        "coverage_targets": {
            "sample_node_counts": dict(node_counts),
            "sample_decision_counts": dict(decisions),
            "minimum_clean_coverage_before_integration": 0.8,
        },
    }


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_parser_backed_dynamic_expression_design_review"
    spec = reports_dir / f"{stamp}_ck3_dynamic_expression_parser_spec.json"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), spec


def validate_pending(conn: sqlite3.Connection, run_id: int, ids: list[int]) -> None:
    if not ids:
        return
    ph = ",".join("?" for _ in ids)
    count = conn.execute(
        f"SELECT COUNT(*) FROM segment_state_items WHERE run_id=? AND segment_id IN ({ph}) AND state_group='pending' AND COALESCE(is_closed,0)=0",
        (run_id, *ids),
    ).fetchone()[0]
    if count != len(set(ids)):
        raise SystemExit(f"pending validation mismatch: {count}/{len(set(ids))}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only parser-backed dynamic expression design review.")
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    parser.add_argument("--ledger-run-id", required=True, type=int)
    parser.add_argument("--router-jsonl", required=True, type=Path)
    parser.add_argument("--validation-jsonl", required=True, type=Path)
    parser.add_argument("--limit", type=int, default=360)
    args = parser.parse_args()

    # Validate source JSONLs are parseable; routing is reconstructed from DB to avoid depending on sampled artifacts.
    for path in (args.router_jsonl, args.validation_jsonl):
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    json.loads(line)

    conn = connect_readonly()
    router.fetch_run(conn, "segment_state_runs", args.segment_state_run_id)
    router.fetch_run(conn, "ml_issue_ledger_runs", args.ledger_run_id)
    pending_rows = router.fetch_pending_rows(conn, args.segment_state_run_id, args.ledger_run_id)
    grouped = router.group_by_segment(pending_rows)
    routed = router.route_segments(grouped)
    samples = sample_groups(routed, grouped, args.limit)
    ids = [int(item["segment_id"]) for item in samples]
    validate_pending(conn, args.segment_state_run_id, ids)

    results: list[dict[str, Any]] = []
    node_counts: Counter[str] = Counter()
    unknown_counts: Counter[str] = Counter()
    component_counts: Counter[str] = Counter()
    decision_counts: Counter[str] = Counter()
    for item in samples:
        old_text, confirmed_text, output_text, blob = text_blob(item, grouped)
        nodes, unknowns = parse_nodes(blob)
        decision, component, confidence, rationale = classify(item, nodes, unknowns, blob)
        node_counts.update(nodes)
        unknown_counts.update(unknowns)
        component_counts[component] += 1
        decision_counts[decision] += 1
        results.append(
            {
                "record_type": "sample_parse",
                "segment_id": int(item["segment_id"]),
                "relative_path": item["relative_path"],
                "source_key": item["source_key"],
                "families_open": item["families"],
                "primary_lane": item["primary_lane"],
                "secondary_lanes": item["secondary_lanes"],
                "sample_bucket": item["sample_bucket"],
                "old_text": old_text,
                "confirmed_text": confirmed_text,
                "output_text": output_text,
                "detected_node_types": nodes,
                "unknown_patterns": unknowns,
                "parser_design_decision": decision,
                "recommended_next_component": component,
                "parse_confidence": confidence,
                "rationale": rationale,
            }
        )

    clean = decision_counts["parser_coverage_clean"]
    unknown = decision_counts["parser_unparsed_unknown_dynamic"]
    coverage_pct = round((len(results) - unknown) / len(results) * 100 if results else 0, 2)
    txt_path, jsonl_path, spec_path = output_paths()
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps({"record_type": "coverage_summary", "sampled": len(results), "clean": clean, "unknown": unknown, "coverage_pct": coverage_pct, "decision_counts": dict(decision_counts)}, ensure_ascii=False, sort_keys=True) + "\n")
        for node, count in node_counts.most_common():
            handle.write(json.dumps({"record_type": "node_type_count", "node_type": node, "count": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for pattern, count in unknown_counts.most_common(25):
            examples = [row["segment_id"] for row in results if pattern in row["unknown_patterns"]][:5]
            handle.write(json.dumps({"record_type": "unknown_pattern", "pattern": pattern, "count": count, "examples": examples}, ensure_ascii=False, sort_keys=True) + "\n")
        for priority, (component, count) in enumerate(component_counts.most_common(), 1):
            handle.write(json.dumps({"record_type": "component_recommendation", "component": component, "estimated_coverage": count, "priority": priority}, ensure_ascii=False, sort_keys=True) + "\n")
        strategies = [
            ("chat_exec_requirement_effect_router_validation_prompt.md", "validate the second override before parser integration"),
            ("chat_exec_macro_lane_router_readonly_component_spec_prompt.md", "materialize router+gender override as read-only component spec"),
            ("chat_exec_dynamic_parser_unknown_pattern_audit_prompt.md", "audit unknown_dynamic/Custom2/Var patterns before integration"),
        ]
        for priority, (prompt, rationale) in enumerate(strategies, 1):
            handle.write(json.dumps({"record_type": "strategy", "priority": priority, "next_prompt": prompt, "rationale": rationale}, ensure_ascii=False, sort_keys=True) + "\n")
        for row in results:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("Parser-backed dynamic expression design review\n\n")
        handle.write(f"total_sampled: {len(results)}\n")
        handle.write(f"coverage_pct_without_unknown: {coverage_pct}\n")
        handle.write(f"gender_override_first: {decision_counts['parser_needs_gender_override_first']}\n")
        handle.write(f"requirement_effect_override_first: {decision_counts['parser_needs_requirement_effect_override_first']}\n")
        handle.write("parser_design_decision_counts:\n")
        for decision, count in decision_counts.most_common():
            handle.write(f"- {decision}: {count}\n")
        handle.write("\nTop node types:\n")
        for node, count in node_counts.most_common(15):
            handle.write(f"- {node}: {count}\n")
        handle.write("\nTop unknown patterns:\n")
        for pattern, count in unknown_counts.most_common(10):
            handle.write(f"- {pattern}: {count}\n")
        handle.write("\nComponentes recomendados:\n")
        for component, count in component_counts.most_common():
            handle.write(f"- {component}: {count}\n")
        handle.write("\nAnalise\n")
        handle.write("- O parser dinamico ainda e componente reutilizavel central, mas deve entrar depois dos overrides gender/local-player e requirement/effect.\n")
        handle.write("- Node types prioritarios: bracket_expression, variable, scope_getter, custom_loc, formatting_tag e concept_link.\n")
        handle.write("- O resultado ainda e camada de roteamento/parser; nao abre lifecycle/apply diretamente.\n")
        handle.write("- Proximo prompt recomendado: requirement/effect router validation antes de integrar parser generico.\n")
        handle.write("\nProximos prompts\n")
        handle.write("1. chat_exec_requirement_effect_router_validation_prompt.md\n")
        handle.write("2. chat_exec_macro_lane_router_readonly_component_spec_prompt.md\n")
        handle.write("3. chat_exec_dynamic_parser_unknown_pattern_audit_prompt.md\n")
    spec = build_spec(args.segment_state_run_id, args.ledger_run_id, node_counts, decision_counts)
    with spec_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(spec, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    print(f"txt: {txt_path}")
    print(f"jsonl: {jsonl_path}")
    print(f"spec: {spec_path}")
    print(f"total_sampled: {len(results)}")
    print(f"coverage_pct_without_unknown: {coverage_pct}")
    print("top_node_types:")
    for node, count in node_counts.most_common(8):
        print(f"  {node}: {count}")
    print("decision_counts:")
    for decision, count in decision_counts.most_common():
        print(f"  {decision}: {count}")


if __name__ == "__main__":
    main()
