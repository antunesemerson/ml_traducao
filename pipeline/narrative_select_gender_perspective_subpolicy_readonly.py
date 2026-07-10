from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "narrative_select_gender_perspective_subpolicy_readonly_v1"
DEFAULT_INPUT = Path("reports/20260703_195859_357117_narrative_select_conditional_overlap_review_readonly.jsonl")
DEFAULT_RUN_ID = 585
TARGET_DECISION = "subpolicy_select_gender_perspective_read_only"

SELECT_SIGNATURE_RE = re.compile(r"\[Select_CString\(([^\]]+)\)\]")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only split-only subpolicy for Select_CString gender/perspective.")
    parser.add_argument("--input-jsonl", type=Path, default=DEFAULT_INPUT)
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


def select_signature(text: str) -> str:
    match = SELECT_SIGNATURE_RE.search(text or "")
    if not match:
        return ""
    signature = match.group(0)
    signature = re.sub(r"'[^']*'", "'...'", signature)
    signature = re.sub(r"\s+", " ", signature)
    return signature[:220]


def choose_route(row: dict[str, Any]) -> tuple[str, str]:
    flags = set(row.get("overlap_flags") or [])
    if "multiline_select" in flags:
        return "hold_select_gender_perspective_parser_later", "multiline_select_requires_parser_later"
    if "select_player_or_local_perspective" in flags:
        return "route_select_cstring_gender_perspective_player_overlap", "local_player_or_player_perspective_overlap"
    if "select_plus_es_helper" in flags:
        return "route_select_cstring_gender_perspective_with_es_helper", "select_cstring_plus_es_helper"
    if "select_plus_getter" in flags:
        return "route_select_cstring_gender_perspective_with_getter", "select_cstring_plus_getter"
    if row.get("select_class") == "select_cstring_multiple":
        return "route_select_cstring_gender_perspective_multiple", "multiple_select_cstring_gender_perspective"
    if int(row.get("high_issue_count") or 0) > 0:
        return "hold_select_gender_perspective_needs_context", "high_issue_context_required"
    return "route_select_cstring_gender_perspective_multiple", "default_select_gender_perspective"


def record(row: dict[str, Any]) -> dict[str, Any]:
    route, reason = choose_route(row)
    text = str(row.get("output_text") or row.get("confirmed_text") or row.get("source_text") or "")
    return {
        "source": SOURCE,
        "record_type": "narrative_select_gender_perspective_subpolicy_item",
        "segment_id": int(row.get("segment_id") or 0),
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "route": route,
        "route_reason": reason,
        "select_signature": select_signature(text),
        "select_class": row.get("select_class"),
        "overlap_flags": row.get("overlap_flags") or [],
        "select_cstring_count": int(row.get("select_cstring_count") or 0),
        "select_localization_count": int(row.get("select_localization_count") or 0),
        "local_player_count": int(row.get("local_player_count") or 0),
        "es_helper_count": int(row.get("es_helper_count") or 0),
        "getter_count": int(row.get("getter_count") or 0),
        "context_around_select": row.get("context_around_select"),
        "getter_tokens": row.get("getter_tokens") or [],
        "token_surface": row.get("token_surface"),
        "open_issue_count": int(row.get("open_issue_count") or 0),
        "high_issue_count": int(row.get("high_issue_count") or 0),
        "confirmed_matches_output": int(row.get("confirmed_matches_output") or 0),
        "needs_output_apply": int(row.get("needs_output_apply") or 0),
        "issue_families": row.get("issue_families") or "",
        "issue_kinds": row.get("issue_kinds") or "",
        "source_text": row.get("source_text"),
        "output_text": row.get("output_text"),
        "confirmed_text": row.get("confirmed_text"),
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


def examples(rows: list[dict[str, Any]], limit: int = 5) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in sorted(rows, key=lambda r: (r["route"], -r["high_issue_count"], r["segment_id"])):
        if len(grouped[row["route"]]) >= limit:
            continue
        grouped[row["route"]].append(
            {
                "segment_id": row["segment_id"],
                "source_key": row["source_key"],
                "select_signature": row["select_signature"],
                "overlap_flags": row["overlap_flags"],
                "route_reason": row["route_reason"],
            }
        )
    return dict(grouped)


def top_signatures(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    counters: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        counters[row["route"]][row["select_signature"] or "no_signature"] += 1
    return {route: dict(counter.most_common(12)) for route, counter in sorted(counters.items())}


def recommendation(route_counts: Counter[str]) -> str:
    player = route_counts.get("route_select_cstring_gender_perspective_player_overlap", 0)
    es = route_counts.get("route_select_cstring_gender_perspective_with_es_helper", 0)
    getter = route_counts.get("route_select_cstring_gender_perspective_with_getter", 0)
    if player >= max(es, getter):
        return "Primeira metadata parser route: player/local perspective overlap, mantendo split-only e sem candidate generation."
    if es >= getter:
        return "Primeira metadata parser route: Select_CString + ES helper, split-only/read-only."
    return "Primeira metadata parser route: Select_CString + getter, split-only/read-only."


def markdown(summary: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    lines = [
        "# Select Gender/Perspective Subpolicy Read-Only",
        "",
        f"- Segment-state run base: {summary['segment_state_run_id']}",
        f"- Entrada: `{summary['input_jsonl']}`",
        f"- Registros roteados: {summary['record_count']}",
        "- Acoes: read-only; sem candidato, apply, ingest, issue closure, lifecycle/materializer, segment-state, reindex ou producao full.",
        "",
        "## Rotas",
    ]
    for route, count in summary["route_counts"].items():
        lines.append(f"- {route}: {count}")
    lines.extend(["", "## Top Padroes Select_CString"])
    for route, patterns in summary["top_select_signatures_by_route"].items():
        compact = ", ".join(f"{sig} ({count})" for sig, count in list(patterns.items())[:4])
        lines.append(f"- {route}: {compact or 'sem assinatura'}")
    lines.extend(["", "## Recomendacao"])
    lines.append(summary["single_operational_recommendation"])
    lines.extend(["", "## Exemplos Por Rota"])
    for route, items in summary["examples_by_route"].items():
        lines.append(f"### {route}")
        for item in items[:4]:
            lines.append(f"- {item['segment_id']} | {item['source_key']} | {item['route_reason']} | {item['select_signature']}")
    lines.append("")
    return "\n".join(lines)


def build(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    input_rows = [row for row in read_jsonl(args.input_jsonl) if row.get("review_decision") == TARGET_DECISION]
    rows = [record(row) for row in input_rows]
    route_counts = Counter(row["route"] for row in rows)
    flag_counts = Counter(flag for row in rows for flag in row["overlap_flags"])
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_split_only_select_gender_perspective_subpolicy",
        "segment_state_run_id": args.run_id,
        "input_jsonl": str(args.input_jsonl),
        "scope": {
            "included_review_decision": TARGET_DECISION,
            "expected_count": 163,
            "excluded_review_decisions": ["parser_later_multiline_select", "subpolicy_select_player_perspective_read_only"],
            "candidate_generation_allowed": False,
            "apply_allowed": False,
            "learning_ingest_allowed": False,
            "issue_closure_allowed": False,
            "lifecycle_or_materializer_allowed": False,
            "segment_state_allowed": False,
            "reindex_allowed": False,
            "production_full_allowed": False,
        },
        "record_count": len(rows),
        "expected_count_ok": len(rows) == 163,
        "route_counts": dict(route_counts.most_common()),
        "overlap_flag_counts": dict(flag_counts.most_common()),
        "top_select_signatures_by_route": top_signatures(rows),
        "examples_by_route": examples(rows),
        "first_parser_metadata_route": recommendation(route_counts).split(":", 1)[-1].strip().split(",", 1)[0],
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
        "single_operational_recommendation": recommendation(route_counts),
    }
    return rows, summary, markdown(summary, rows)


def write(rows: list[dict[str, Any]], summary: dict[str, Any], md: str) -> dict[str, str]:
    base = reports_dir() / f"{stamp()}_narrative_select_gender_perspective_subpolicy_readonly"
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
    print(f"record_count={summary['record_count']}")
    print(f"expected_count_ok={summary['expected_count_ok']}")
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
