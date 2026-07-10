from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import narrative_dynamic_getter_gender_architecture_review as review


SOURCE = "narrative_dynamic_getter_gender_splitter_readonly_v1"
DEFAULT_REVIEW_JSONL = Path("reports/20260703_184835_428743_narrative_dynamic_getter_gender_architecture_review.jsonl")
DEFAULT_FULL_INPUT = Path("reports/20260703_182038_026776_release_readiness_post544_diagnostic.jsonl")
DEFAULT_RUN_ID = 585

ROUTES = [
    "terminal_parser_later_gender_agreement_dependency",
    "route_faith_culture_getter_role_splitter_read_only",
    "route_runtime_name_getter_preserve_splitter_read_only",
    "hold_actor_pronoun_context",
    "hold_literal_spanish_near_getter",
    "hold_getter_relation_possessive",
    "hold_select_or_conditional_overlap",
    "hold_parser_later_context",
]


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only splitter for narrative dynamic getter + gender/perspective release blockers."
    )
    parser.add_argument("--review-jsonl", type=Path, default=DEFAULT_REVIEW_JSONL)
    parser.add_argument("--full-input-jsonl", type=Path, default=DEFAULT_FULL_INPUT)
    parser.add_argument("--run-id", type=int, default=DEFAULT_RUN_ID)
    return parser.parse_args()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    resolved = db.project_path(path)
    rows: list[dict[str, Any]] = []
    with resolved.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def load_universe(args: argparse.Namespace) -> tuple[list[dict[str, Any]], str]:
    full_path = db.project_path(args.full_input_jsonl)
    if full_path.exists():
        rows = read_jsonl(args.full_input_jsonl)
        return [review.record_from(row) for row in rows if review.qualify(row)], str(args.full_input_jsonl)
    review_path = db.project_path(args.review_jsonl)
    if not review_path.exists():
        raise SystemExit(f"missing input: {args.review_jsonl}")
    return read_jsonl(args.review_jsonl), str(args.review_jsonl)


def choose_route(record: dict[str, Any]) -> tuple[str, str]:
    roles = set(record.get("architecture_roles") or [])
    if "Select_CString/SelectLocalization overlap" in roles:
        return "hold_select_or_conditional_overlap", "select_or_conditional_overlap_requires_parser"
    if "getter_relation/possessive" in roles or "actor_pronoun_object" in roles:
        return "hold_getter_relation_possessive", "relation_or_possessive_requires_context"
    if "actor_pronoun_subject" in roles:
        return "hold_actor_pronoun_context", "actor_pronoun_subject_requires_perspective_context"
    if "literal Spanish residue near getter" in roles:
        return "hold_literal_spanish_near_getter", "spanish_residue_near_getter_requires_context"
    if "gendered_adjective" in roles or "gendered_noun" in roles:
        return (
            "terminal_parser_later_gender_agreement_dependency",
            "parser_metadata_212_all_context_or_literal_spanish_overlap",
        )
    if "getter_faith/culture" in roles:
        return "route_faith_culture_getter_role_splitter_read_only", "faith_culture_getter_role_split_only"
    if "getter_person_name" in roles:
        return "route_runtime_name_getter_preserve_splitter_read_only", "runtime_name_getter_preserve_split_only"
    if "needs_parser_later" in roles or "human_context_required" in roles:
        return "hold_parser_later_context", "parser_later_or_high_issue_context"
    return "hold_parser_later_context", "unclassified_getter_gender_context"


def routed_record(record: dict[str, Any]) -> dict[str, Any]:
    route, reason = choose_route(record)
    return {
        "source": SOURCE,
        "record_type": "narrative_dynamic_getter_gender_splitter_readonly_item",
        "segment_id": record.get("segment_id"),
        "relative_path": record.get("relative_path"),
        "source_key": record.get("source_key"),
        "route": route,
        "route_reason": reason,
        "architecture_roles": record.get("architecture_roles") or [],
        "primary_route_from_review": record.get("primary_route"),
        "parser_recommendation_from_review": record.get("parser_recommendation"),
        "token_surface": record.get("token_surface"),
        "surface_flags": record.get("surface_flags") or [],
        "getter_tokens": record.get("getter_tokens") or [],
        "token_count": record.get("token_count"),
        "dominant_issue_family": record.get("dominant_issue_family") or "",
        "issue_families": record.get("issue_families") or "",
        "issue_kinds": record.get("issue_kinds") or "",
        "open_issue_count": int(record.get("open_issue_count") or 0),
        "high_issue_count": int(record.get("high_issue_count") or 0),
        "spanish_residue_visible": bool(record.get("spanish_residue_visible")),
        "gender_or_perspective": bool(record.get("gender_or_perspective")),
        "confirmed_matches_output": int(record.get("confirmed_matches_output") or 0),
        "needs_output_apply": int(record.get("needs_output_apply") or 0),
        "source_text": record.get("source_text"),
        "output_text": record.get("output_text"),
        "confirmed_text": record.get("confirmed_text"),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "learning_ingest_count": 0,
        "issue_closure_count": 0,
        "lifecycle_count": 0,
        "materializer_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
    }


def top_by_route(rows: list[dict[str, Any]], field: str, limit: int = 12) -> dict[str, dict[str, int]]:
    counters: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        values = row.get(field) or []
        if isinstance(values, str):
            values = [values]
        for value in values:
            if value:
                counters[row["route"]][str(value)] += 1
    return {route: dict(counter.most_common(limit)) for route, counter in sorted(counters.items())}


def examples_by_route(rows: list[dict[str, Any]], limit: int = 8) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    sorted_rows = sorted(rows, key=lambda r: (r["route"], -r["high_issue_count"], -r["open_issue_count"], r["segment_id"]))
    for row in sorted_rows:
        if len(grouped[row["route"]]) >= limit:
            continue
        grouped[row["route"]].append(
            {
                "segment_id": row["segment_id"],
                "source_key": row["source_key"],
                "relative_path": row["relative_path"],
                "roles": row["architecture_roles"],
                "getter_tokens": row["getter_tokens"][:5],
                "route_reason": row["route_reason"],
            }
        )
    return dict(grouped)


def first_parser_metadata_route(route_counts: Counter[str]) -> str:
    for route in (
        "route_faith_culture_getter_role_splitter_read_only",
        "route_runtime_name_getter_preserve_splitter_read_only",
    ):
        if route_counts.get(route, 0) > 0:
            return route
    if route_counts.get("terminal_parser_later_gender_agreement_dependency", 0) > 0:
        return "terminal_parser_later_gender_agreement_dependency"
    return "hold_parser_later_context"


def markdown(summary: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    lines = [
        "# Narrative Dynamic Getter + Gender/Perspective Splitter Read-Only",
        "",
        f"- Segment-state run base: {summary['segment_state_run_id']}",
        f"- Universo roteado: {summary['routed_count']}",
        f"- Entrada efetiva: `{summary['effective_input_jsonl']}`",
        "- Acoes: read-only; sem candidato, apply, ingest, issue closure, lifecycle/materializer, segment-state, reindex ou producao full.",
        "",
        "## Contagem Por Rota",
    ]
    for route, count in summary["route_counts"].items():
        lines.append(f"- {route}: {count}")
    lines.extend(["", "## Primeira Subrota Recomendada Para Parser Metadata"])
    lines.append(summary["first_parser_metadata_route"])
    lines.extend(["", "## Top Tokens/Getters Por Rota"])
    for route, tokens in summary["top_getter_tokens_by_route"].items():
        compact = ", ".join(f"{token} ({count})" for token, count in list(tokens.items())[:8])
        lines.append(f"- {route}: {compact or 'sem getter token dominante'}")
    lines.extend(["", "## Recomendacao Operacional"])
    lines.append(summary["single_operational_recommendation"])
    lines.extend(["", "## Exemplos Por Rota"])
    for route, examples in summary["examples_by_route"].items():
        lines.append(f"### {route}")
        for item in examples[:5]:
            tokens = ", ".join(item["getter_tokens"][:3])
            lines.append(f"- {item['segment_id']} | {item['source_key']} | {item['route_reason']} | {tokens}")
    lines.append("")
    return "\n".join(lines)


def build(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    records, effective_input = load_universe(args)
    routed = [routed_record(record) for record in records]
    route_counts = Counter(row["route"] for row in routed)
    role_counts = Counter(role for row in routed for role in row["architecture_roles"])
    token_surface_counts = Counter(row["token_surface"] for row in routed)
    route_issue_counts = defaultdict(Counter)
    for row in routed:
        route_issue_counts[row["route"]][row["dominant_issue_family"] or "none"] += 1
    first_route = first_parser_metadata_route(route_counts)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_splitter_route_only",
        "segment_state_run_id": args.run_id,
        "review_jsonl": str(args.review_jsonl),
        "effective_input_jsonl": effective_input,
        "scope": {
            "visibility_group": "narrative_events",
            "surfaces": ["dynamic_getter", "gender_perspective", "dynamic_getter+gender_perspective"],
            "excluded_segment_ids": [120831, 127174],
            "routes": ROUTES,
        },
        "routed_count": len(routed),
        "route_counts": dict(route_counts.most_common()),
        "architecture_role_counts": dict(role_counts.most_common()),
        "token_surface_counts": dict(token_surface_counts.most_common()),
        "dominant_issue_family_counts_by_route": {
            route: dict(counter.most_common(10)) for route, counter in sorted(route_issue_counts.items())
        },
        "top_getter_tokens_by_route": top_by_route(routed, "getter_tokens"),
        "top_roles_by_route": top_by_route(routed, "architecture_roles"),
        "examples_by_route": examples_by_route(routed),
        "first_parser_metadata_route": first_route,
        "parser_metadata_recommendation": {
            "agent_key": first_route,
            "agent_type": "subcoordinator",
            "operational_state": "shadow",
            "decision_role": "route_and_split",
            "parent_agent_key": "narrative_events_dynamic_getter_gender_parser_policy",
            "scope_group": "release_readiness_narrative_events",
            "candidate_generation_allowed": False,
            "auto_apply_allowed": False,
            "lifecycle_allowed": False,
            "production_release_allowed": False,
        },
        "candidate_generation_count": 0,
        "apply_count": 0,
        "learning_ingest_count": 0,
        "issue_closure_count": 0,
        "lifecycle_count": 0,
        "materializer_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "single_operational_recommendation": (
            f"Use {first_route} como primeira subrota de metadata/parser em modo shadow split-only. "
            "Manter todos os holds como terminais operacionais ate haver parser especifico; nao gerar candidatos nem corrigir texto."
        ),
    }
    return routed, summary, markdown(summary, routed)


def write(rows: list[dict[str, Any]], summary: dict[str, Any], md: str) -> dict[str, str]:
    base = reports_dir() / f"{stamp()}_narrative_dynamic_getter_gender_splitter_readonly"
    md_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    jsonl_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    summary["output_files"] = {"markdown": str(md_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    md_path.write_text(md, encoding="utf-8")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return summary["output_files"]


def main() -> None:
    args = parse_args()
    rows, summary, md = build(args)
    outputs = write(rows, summary, md)
    print(f"markdown={outputs['markdown']}")
    print(f"jsonl={outputs['jsonl']}")
    print(f"summary={outputs['summary']}")
    print(f"routed_count={summary['routed_count']}")
    print(f"route_counts={json.dumps(summary['route_counts'], ensure_ascii=False)}")
    print(f"first_parser_metadata_route={summary['first_parser_metadata_route']}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("learning_ingest_count=0")
    print("issue_closure_count=0")
    print("lifecycle_count=0")
    print("materializer_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
