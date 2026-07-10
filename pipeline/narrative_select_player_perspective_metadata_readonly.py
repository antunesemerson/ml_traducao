from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "narrative_select_player_perspective_metadata_readonly_v1"
DEFAULT_INPUT = Path("reports/20260703_200250_925787_narrative_select_gender_perspective_subpolicy_readonly.jsonl")
DEFAULT_RUN_ID = 585
TARGET_ROUTE = "route_select_cstring_gender_perspective_player_overlap"

SELECT_RE = re.compile(r"\[Select_CString\((.*?)\)\]")
QUOTED_RE = re.compile(r"'([^']*)'")
GETTER_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+)\b")
ES_RE = re.compile(r"ES_[A-Za-z]+|Custom\('ES_[^']+'\)")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only metadata for Select_CString local-player perspective route.")
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


def select_expressions(text: str) -> list[str]:
    return [match.group(0) for match in SELECT_RE.finditer(text or "")]


def select_condition(expr: str) -> str:
    inner = expr[len("[Select_CString(") : -2] if expr.startswith("[Select_CString(") else expr
    first_quote = inner.find("'")
    condition = inner[:first_quote].rstrip(" ,") if first_quote >= 0 else inner
    return re.sub(r"\s+", " ", condition).strip()


def variants(expr: str) -> list[str]:
    return QUOTED_RE.findall(expr or "")


def term_from_variants(values: list[str]) -> str:
    if not values:
        return ""
    short = sorted(values, key=len)[0]
    return short[:80]


def dependency(condition: str, values: list[str]) -> str:
    low_values = " ".join(values).lower()
    if "Or(" in condition or "And(" in condition:
        return "mixed_player_and_gender"
    if "IsLocalPlayer" in condition and any(marker in low_values for marker in ("seu ", "sua ", "te ", "você", "tu ", "seu personagem")):
        return "local_player_second_person"
    if "IsLocalPlayer" in condition:
        return "local_player_first_person"
    if "IsFemale" in condition:
        return "third_person_gendered"
    return "unknown"


def risk(row: dict[str, Any], condition: str) -> str:
    flags = set(row.get("overlap_flags") or [])
    if "\\n" in str(row.get("output_text") or "") or "\n" in str(row.get("output_text") or ""):
        return "parser_later"
    if "select_plus_es_helper" in flags or ES_RE.search(str(row.get("output_text") or "")):
        return "es_helper_overlap"
    if "select_plus_getter" in flags:
        return "getter_overlap"
    if "TARGET_CHARACTER" in condition or "recipient" in condition.lower() or "target" in condition.lower():
        return "needs_context_recipient"
    if "CHARACTER" in condition or "ROOT.Char" in condition or "host" in condition:
        return "needs_context_actor"
    if "IsLocalPlayer" in condition:
        return "local_player_perspective_hold"
    return "metadata_only_ok"


def context_type(row: dict[str, Any]) -> str:
    if "\\n" in str(row.get("output_text") or "") or "\n" in str(row.get("output_text") or ""):
        return "context_expanded"
    if int(row.get("select_cstring_count") or 0) > 3 or int(row.get("getter_count") or 0) > 3:
        return "context_expanded"
    return "single_line"


def record(row: dict[str, Any]) -> dict[str, Any]:
    text = str(row.get("output_text") or row.get("confirmed_text") or row.get("source_text") or "")
    expressions = [expr for expr in select_expressions(text) if "IsLocalPlayer" in expr or "GetPlayer" in expr]
    expr = expressions[0] if expressions else str(row.get("select_signature") or "")
    condition = select_condition(expr)
    values = variants(expr)
    dep = dependency(condition, values)
    return {
        "source": SOURCE,
        "record_type": "narrative_select_player_perspective_metadata_item",
        "segment_id": int(row.get("segment_id") or 0),
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "select_cstring_expression": expr,
        "select_condition": condition,
        "select_variants": values,
        "pt_br_affected_term": term_from_variants(values),
        "has_getter_overlap": "select_plus_getter" in set(row.get("overlap_flags") or []),
        "has_es_helper_overlap": "select_plus_es_helper" in set(row.get("overlap_flags") or []),
        "line_context": context_type(row),
        "risk": risk(row, condition),
        "dependency": dep,
        "select_signature": row.get("select_signature"),
        "overlap_flags": row.get("overlap_flags") or [],
        "select_cstring_count": int(row.get("select_cstring_count") or 0),
        "local_player_count": int(row.get("local_player_count") or 0),
        "getter_count": int(row.get("getter_count") or 0),
        "es_helper_count": int(row.get("es_helper_count") or 0),
        "getter_tokens": row.get("getter_tokens") or [],
        "token_surface": row.get("token_surface"),
        "open_issue_count": int(row.get("open_issue_count") or 0),
        "high_issue_count": int(row.get("high_issue_count") or 0),
        "confirmed_matches_output": int(row.get("confirmed_matches_output") or 0),
        "needs_output_apply": int(row.get("needs_output_apply") or 0),
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


def examples(rows: list[dict[str, Any]], field: str, limit: int = 5) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in sorted(rows, key=lambda r: (r[field], -r["high_issue_count"], r["segment_id"])):
        if len(grouped[row[field]]) >= limit:
            continue
        grouped[row[field]].append(
            {
                "segment_id": row["segment_id"],
                "source_key": row["source_key"],
                "condition": row["select_condition"],
                "variants": row["select_variants"],
                "risk": row["risk"],
                "dependency": row["dependency"],
            }
        )
    return dict(grouped)


def top_conditions(rows: list[dict[str, Any]]) -> dict[str, int]:
    return dict(Counter(row["select_condition"] or "unknown" for row in rows).most_common(25))


def recommendation(summary: dict[str, Any]) -> str:
    metadata_ok = summary["risk_counts"].get("metadata_only_ok", 0)
    dominant_risk = next(iter(summary["risk_counts"]), "unknown")
    if metadata_ok >= 20:
        return "Ha subrota metadata estavel para validacao read-only, mas ainda sem candidate generation."
    return (
        f"Manter como parser-later/metadata terminal por enquanto; risco dominante: {dominant_risk}. "
        "A proxima etapa util e desenhar parser de perspectiva local/player antes de qualquer pacote humano."
    )


def markdown(summary: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    lines = [
        "# Select_CString Player Perspective Metadata Read-Only",
        "",
        f"- Segment-state run base: {summary['segment_state_run_id']}",
        f"- Entrada: `{summary['input_jsonl']}`",
        f"- Registros: {summary['record_count']}",
        "- Acoes: read-only; sem candidato, apply, ingest, issue closure, lifecycle/materializer, segment-state, reindex ou producao full.",
        "",
        "## Dependencia",
    ]
    for key, count in summary["dependency_counts"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(["", "## Risco"])
    for key, count in summary["risk_counts"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(["", "## Contexto"])
    for key, count in summary["line_context_counts"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(["", "## Recomendacao"])
    lines.append(summary["single_operational_recommendation"])
    lines.extend(["", "## Top Condicoes"])
    for key, count in list(summary["top_conditions"].items())[:12]:
        lines.append(f"- {key}: {count}")
    lines.extend(["", "## Exemplos Por Risco"])
    for key, items in summary["examples_by_risk"].items():
        lines.append(f"### {key}")
        for item in items[:4]:
            lines.append(f"- {item['segment_id']} | {item['source_key']} | {item['condition']} | {item['variants']}")
    lines.append("")
    return "\n".join(lines)


def build(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    input_rows = [row for row in read_jsonl(args.input_jsonl) if row.get("route") == TARGET_ROUTE]
    rows = [record(row) for row in input_rows]
    dependency_counts = Counter(row["dependency"] for row in rows)
    risk_counts = Counter(row["risk"] for row in rows)
    context_counts = Counter(row["line_context"] for row in rows)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_select_player_perspective_metadata",
        "segment_state_run_id": args.run_id,
        "input_jsonl": str(args.input_jsonl),
        "scope": {
            "route": TARGET_ROUTE,
            "expected_count": 94,
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
        "expected_count_ok": len(rows) == 94,
        "dependency_counts": dict(dependency_counts.most_common()),
        "risk_counts": dict(risk_counts.most_common()),
        "line_context_counts": dict(context_counts.most_common()),
        "top_conditions": top_conditions(rows),
        "examples_by_risk": examples(rows, "risk"),
        "examples_by_dependency": examples(rows, "dependency"),
        "stable_metadata_subroute_exists": risk_counts.get("metadata_only_ok", 0) >= 20,
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
    }
    summary["single_operational_recommendation"] = recommendation(summary)
    return rows, summary, markdown(summary, rows)


def write(rows: list[dict[str, Any]], summary: dict[str, Any], md: str) -> dict[str, str]:
    base = reports_dir() / f"{stamp()}_narrative_select_player_perspective_metadata_readonly"
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
    print(f"dependency_counts={json.dumps(summary['dependency_counts'], ensure_ascii=False)}")
    print(f"risk_counts={json.dumps(summary['risk_counts'], ensure_ascii=False)}")
    print(f"line_context_counts={json.dumps(summary['line_context_counts'], ensure_ascii=False)}")
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
