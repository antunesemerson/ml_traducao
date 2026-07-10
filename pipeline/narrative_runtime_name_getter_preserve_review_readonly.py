from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "narrative_runtime_name_getter_preserve_review_readonly_v1"
DEFAULT_INPUT = Path("reports/20260703_193709_147411_narrative_dynamic_getter_gender_splitter_readonly.jsonl")
DEFAULT_RUN_ID = 585
TARGET_ROUTE = "route_runtime_name_getter_preserve_splitter_read_only"

TOKEN_RE = re.compile(r"\[[^\]]+\]")
CHARACTER_RE = re.compile(
    r"Get(?:FirstName|FullName|ShortUIName|TitledFirstName|NameNoTooltip|UIName)(?:\||\]|\()|"
    r"\[[a-zA-Z0-9_]+\.(?:GetFirstName|GetShortUIName|GetTitledFirstName)"
)
ACTIVITY_ARTIFACT_RE = re.compile(
    r"Get(?:ActivityType|Trait|Modifier|Situation|Scheme|AccoladeType|VassalStance|CourtPosition|DomicileBuilding|Building|TravelOption|Intent)"
    r"|GetInvolvedActivityIntent|GetNameWithTooltip|SCOPE\.ScriptValue"
)
TITLE_REALM_RE = re.compile(
    r"GetTitleByKey|GetNameNoTier|GetPrimaryTitle|GetCapitalLocation|GetCurrentLocation|employer_location_destination|destination_\d+|"
    r"GetDefender\.GetName|GetLiege"
)
CONTEXT_RE = re.compile(r"Concept\(|Glossary\(|Select_CString|SelectLocalization|SCOPE\.ScriptValue|\\n|\n")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only review for runtime-name getter preserve route.")
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


def blob(row: dict[str, Any]) -> str:
    return "\n".join(
        str(row.get(key) or "")
        for key in ("source_text", "output_text", "confirmed_text", "source_key", "issue_kinds", "issue_families")
    )


def main_token(row: dict[str, Any]) -> str:
    tokens = row.get("getter_tokens") or []
    for token in tokens:
        if any(marker in token for marker in ("GetName", "GetNameNoTier", "GetFirstName", "GetShortUIName", "GetTitledFirstName", "ScriptValue")):
            return token
    return tokens[0] if tokens else ""


def classify_runtime(row: dict[str, Any]) -> str:
    text = blob(row)
    token = main_token(row)
    if CONTEXT_RE.search(text) or int(row.get("high_issue_count") or 0) > 0:
        # Keep the operational class conservative; detailed getter family is still recorded below.
        return "needs_context"
    if TITLE_REALM_RE.search(text) or TITLE_REALM_RE.search(token):
        return "title_or_realm_runtime_name"
    if ACTIVITY_ARTIFACT_RE.search(text) or ACTIVITY_ARTIFACT_RE.search(token):
        return "activity_or_artifact_runtime_name"
    if CHARACTER_RE.search(text) or CHARACTER_RE.search(token):
        return "character_name_getter"
    return "unknown_runtime_name"


def getter_family(row: dict[str, Any]) -> str:
    text = blob(row)
    token = main_token(row)
    if TITLE_REALM_RE.search(text) or TITLE_REALM_RE.search(token):
        return "title_or_realm_runtime_name"
    if ACTIVITY_ARTIFACT_RE.search(text) or ACTIVITY_ARTIFACT_RE.search(token):
        return "activity_or_artifact_runtime_name"
    if CHARACTER_RE.search(text) or CHARACTER_RE.search(token):
        return "character_name_getter"
    return "unknown_runtime_name"


def review_decision(row: dict[str, Any], runtime_class: str, family: str) -> str:
    if int(row.get("needs_output_apply") or 0) != 0 or int(row.get("confirmed_matches_output") or 0) != 1:
        return "hold_output_state"
    if int(row.get("high_issue_count") or 0) > 0:
        return "hold_high_issue_context"
    if CONTEXT_RE.search(blob(row)):
        return "hold_parser_context"
    if runtime_class in {
        "character_name_getter",
        "activity_or_artifact_runtime_name",
        "title_or_realm_runtime_name",
    }:
        return "preserve_split_only_metadata_ok"
    return "hold_unknown_runtime_name"


def context_around(text: str, token: str, width: int = 72) -> str:
    if not text or not token:
        return ""
    idx = text.find(token)
    if idx < 0:
        return text[: width * 2].replace("\n", "\\n")
    start = max(0, idx - width)
    end = min(len(text), idx + len(token) + width)
    return text[start:end].replace("\n", "\\n")


def record(row: dict[str, Any]) -> dict[str, Any]:
    token = main_token(row)
    runtime_class = classify_runtime(row)
    family = getter_family(row)
    decision = review_decision(row, runtime_class, family)
    text = str(row.get("output_text") or row.get("confirmed_text") or row.get("source_text") or "")
    return {
        "source": SOURCE,
        "record_type": "narrative_runtime_name_getter_preserve_review_item",
        "segment_id": int(row.get("segment_id") or 0),
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "runtime_class": runtime_class,
        "getter_family": family,
        "review_decision": decision,
        "main_getter_or_token": token,
        "context_around_getter": context_around(text, token),
        "all_getter_tokens": row.get("getter_tokens") or [],
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


def examples(rows: list[dict[str, Any]], field: str, limit: int = 6) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in sorted(rows, key=lambda r: (r[field], -r["high_issue_count"], r["segment_id"])):
        key = row[field]
        if len(grouped[key]) >= limit:
            continue
        grouped[key].append(
            {
                "segment_id": row["segment_id"],
                "source_key": row["source_key"],
                "main_getter_or_token": row["main_getter_or_token"],
                "getter_family": row["getter_family"],
                "review_decision": row["review_decision"],
                "context": row["context_around_getter"],
            }
        )
    return dict(grouped)


def top_tokens_by_class(rows: list[dict[str, Any]], field: str) -> dict[str, dict[str, int]]:
    counters: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        counters[row[field]][row["main_getter_or_token"] or "none"] += 1
    return {key: dict(counter.most_common(12)) for key, counter in sorted(counters.items())}


def recommendation(summary: dict[str, Any]) -> str:
    metadata_ok = summary["review_decision_counts"].get("preserve_split_only_metadata_ok", 0)
    holds = summary["record_count"] - metadata_ok
    if metadata_ok >= 20 and metadata_ok > holds:
        return "A rota pode virar splitter preserve-only, com sublote humano pequeno apenas depois de remover holds de contexto."
    if metadata_ok > 0:
        return (
            "A rota deve ficar como splitter preserve-only read-only. Ha poucos itens metadata_ok; nao usar lifecycle/human review "
            "como frente principal enquanto high/context holds dominarem."
        )
    return "Manter como parser-later/context hold; nao ha sublote seguro para lifecycle ou human review nesta rota."


def markdown(summary: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    lines = [
        "# Narrative Runtime Name Getter Preserve Review",
        "",
        f"- Segment-state run base: {summary['segment_state_run_id']}",
        f"- Entrada: `{summary['input_jsonl']}`",
        f"- Registros da rota: {summary['record_count']}",
        "- Acoes: read-only; sem candidato, apply, ingest, issue closure, lifecycle/materializer, segment-state, reindex ou producao full.",
        "",
        "## Classe Operacional",
    ]
    for key, count in summary["runtime_class_counts"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(["", "## Familia Do Getter"])
    for key, count in summary["getter_family_counts"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(["", "## Decisao De Review"])
    for key, count in summary["review_decision_counts"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(["", "## Recomendacao"])
    lines.append(summary["single_operational_recommendation"])
    lines.extend(["", "## Exemplos Por Classe"])
    for key, items in summary["examples_by_runtime_class"].items():
        lines.append(f"### {key}")
        for item in items[:4]:
            lines.append(
                f"- {item['segment_id']} | {item['source_key']} | {item['main_getter_or_token']} | "
                f"{item['review_decision']}"
            )
    lines.append("")
    return "\n".join(lines)


def build(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    input_rows = [row for row in read_jsonl(args.input_jsonl) if row.get("route") == TARGET_ROUTE]
    rows = [record(row) for row in input_rows]
    runtime_counts = Counter(row["runtime_class"] for row in rows)
    family_counts = Counter(row["getter_family"] for row in rows)
    decision_counts = Counter(row["review_decision"] for row in rows)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_runtime_name_getter_preserve_review",
        "segment_state_run_id": args.run_id,
        "input_jsonl": str(args.input_jsonl),
        "scope": {
            "route": TARGET_ROUTE,
            "expected_count": 204,
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
        "expected_count_ok": len(rows) == 204,
        "runtime_class_counts": dict(runtime_counts.most_common()),
        "getter_family_counts": dict(family_counts.most_common()),
        "review_decision_counts": dict(decision_counts.most_common()),
        "top_tokens_by_runtime_class": top_tokens_by_class(rows, "runtime_class"),
        "top_tokens_by_review_decision": top_tokens_by_class(rows, "review_decision"),
        "examples_by_runtime_class": examples(rows, "runtime_class"),
        "examples_by_review_decision": examples(rows, "review_decision"),
        "safe_lifecycle_or_human_review_sublote_now": decision_counts.get("preserve_split_only_metadata_ok", 0) >= 20
        and decision_counts.get("hold_high_issue_context", 0) == 0,
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
    base = reports_dir() / f"{stamp()}_narrative_runtime_name_getter_preserve_review_readonly"
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
    print(f"runtime_class_counts={json.dumps(summary['runtime_class_counts'], ensure_ascii=False)}")
    print(f"getter_family_counts={json.dumps(summary['getter_family_counts'], ensure_ascii=False)}")
    print(f"review_decision_counts={json.dumps(summary['review_decision_counts'], ensure_ascii=False)}")
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
