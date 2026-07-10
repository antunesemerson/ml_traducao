from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "narrative_faith_culture_getter_role_review_readonly_v1"
DEFAULT_INPUT = Path("reports/20260703_193709_147411_narrative_dynamic_getter_gender_splitter_readonly.jsonl")
DEFAULT_RUN_ID = 585
TARGET_ROUTE = "route_faith_culture_getter_role_splitter_read_only"

ARTICLE_PREP_RE = re.compile(r"\b(d[aeo]s?|aos?|as?|os?|um(?:a|as|s)?|pel[ao]s?)\s+\[[^\]]+\]", re.IGNORECASE)
SPANISH_RE = re.compile(r"\b(el|la|los|las|una?|naci[oó]n|antigu[oa]s?|credo|herej[ií]a)\b", re.IGNORECASE)
GENDER_RE = re.compile(r"ES_OA|ES_XA|ES_EA|ES_ElLa|GetWomanMan|GetAdherentName")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only review for narrative faith/culture getter route.")
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
        if any(marker in token for marker in ("Faith", "Religion", "Culture", "Tradition", "Doctrine")):
            return token
    return tokens[0] if tokens else ""


def classify(row: dict[str, Any]) -> str:
    text = blob(row)
    token = main_token(row)
    if "GetTitleByKey" in text:
        return "title_or_realm_overlap"
    if GENDER_RE.search(text):
        return "gender_agreement_context"
    if row.get("spanish_residue_visible") or SPANISH_RE.search(str(row.get("output_text") or "")):
        return "spanish_literal_overlap"
    if ARTICLE_PREP_RE.search(str(row.get("output_text") or "")):
        return "article_preposition_context"
    if "GetCollectiveNoun" in text:
        return "culture_collective_getter"
    if "GetCulture" in text or "Culture.GetName" in token or "GetCultureTradition" in text:
        return "culture_name_getter"
    if "GetAdjective" in text:
        return "faith_adjective_getter"
    if any(marker in text for marker in ("GetFaith", "GetReligion", "GetFaithByKey", "GetReligionByKey", "FaithDoctrine")):
        return "faith_name_getter"
    if "\n" in str(row.get("output_text") or "") or "\\n" in str(row.get("output_text") or ""):
        return "parser_later"
    return "needs_context"


def review_decision(row: dict[str, Any], cls: str) -> str:
    if int(row.get("needs_output_apply") or 0) != 0 or int(row.get("confirmed_matches_output") or 0) != 1:
        return "hold_output_state"
    if cls in {
        "title_or_realm_overlap",
        "gender_agreement_context",
        "spanish_literal_overlap",
        "article_preposition_context",
        "parser_later",
        "needs_context",
    }:
        return "hold_context_or_overlap"
    if int(row.get("high_issue_count") or 0) > 0:
        return "split_only_high_issue_hold"
    return "absorbed_by_existing_faith_culture_getter_policy"


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
    cls = classify(row)
    decision = review_decision(row, cls)
    text = str(row.get("output_text") or row.get("confirmed_text") or row.get("source_text") or "")
    return {
        "source": SOURCE,
        "record_type": "narrative_faith_culture_getter_role_review_item",
        "segment_id": int(row.get("segment_id") or 0),
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "faith_culture_class": cls,
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


def examples(rows: list[dict[str, Any]], field: str, limit: int = 5) -> dict[str, list[dict[str, Any]]]:
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
                "review_decision": row["review_decision"],
                "context": row["context_around_getter"],
            }
        )
    return dict(grouped)


def top_tokens_by_class(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    counters: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        counters[row["faith_culture_class"]][row["main_getter_or_token"] or "none"] += 1
    return {key: dict(counter.most_common(10)) for key, counter in sorted(counters.items())}


def recommendation(summary: dict[str, Any]) -> str:
    absorbed = summary["review_decision_counts"].get("absorbed_by_existing_faith_culture_getter_policy", 0)
    high_hold = summary["review_decision_counts"].get("split_only_high_issue_hold", 0)
    context_hold = summary["review_decision_counts"].get("hold_context_or_overlap", 0)
    if absorbed >= 10 and context_hold == 0:
        return "Absorver por policy existente de faith/culture getter em modo read-only split-only; sem human review ou lifecycle."
    if high_hold > 0 and context_hold == 0:
        return (
            "A rota e util como subpolicy read-only split-only de faith/culture getter, mas deve permanecer hold por high/context issue; "
            "nao ha sublote seguro para lifecycle ou human review."
        )
    return (
        "Particionar: absorver apenas classes faith/culture puras pela policy existente em shadow/read-only e manter overlaps "
        "artigo/genero/espanhol/titulo em hold especifico."
    )


def markdown(summary: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    lines = [
        "# Narrative Faith/Culture Getter Role Review",
        "",
        f"- Segment-state run base: {summary['segment_state_run_id']}",
        f"- Entrada: `{summary['input_jsonl']}`",
        f"- Registros da rota: {summary['record_count']}",
        "- Acoes: read-only; sem candidato, apply, ingest, issue closure, lifecycle/materializer, segment-state, reindex ou producao full.",
        "",
        "## Classes",
    ]
    for key, count in summary["faith_culture_class_counts"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(["", "## Decisao De Review"])
    for key, count in summary["review_decision_counts"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(["", "## Recomendacao"])
    lines.append(summary["single_operational_recommendation"])
    lines.extend(["", "## Rota Final Recomendada"])
    lines.append(summary["final_route_recommendation"])
    lines.extend(["", "## Exemplos Por Classe"])
    for key, items in summary["examples_by_class"].items():
        lines.append(f"### {key}")
        for item in items[:4]:
            lines.append(
                f"- {item['segment_id']} | {item['source_key']} | {item['main_getter_or_token']} | {item['review_decision']}"
            )
    lines.append("")
    return "\n".join(lines)


def build(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    input_rows = [row for row in read_jsonl(args.input_jsonl) if row.get("route") == TARGET_ROUTE]
    rows = [record(row) for row in input_rows]
    class_counts = Counter(row["faith_culture_class"] for row in rows)
    decision_counts = Counter(row["review_decision"] for row in rows)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_faith_culture_getter_role_review",
        "segment_state_run_id": args.run_id,
        "input_jsonl": str(args.input_jsonl),
        "scope": {
            "route": TARGET_ROUTE,
            "expected_count": 33,
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
        "expected_count_ok": len(rows) == 33,
        "faith_culture_class_counts": dict(class_counts.most_common()),
        "review_decision_counts": dict(decision_counts.most_common()),
        "top_tokens_by_class": top_tokens_by_class(rows),
        "examples_by_class": examples(rows, "faith_culture_class"),
        "examples_by_review_decision": examples(rows, "review_decision"),
        "absorbed_by_existing_policy_count": decision_counts.get("absorbed_by_existing_faith_culture_getter_policy", 0),
        "split_only_preserve_count": decision_counts.get("split_only_high_issue_hold", 0),
        "safe_human_review_sublote_now": False,
        "safe_lifecycle_sublote_now": False,
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
        "final_route_recommendation": (
            "route_faith_culture_getter_existing_policy_absorption_read_only for pure faith/culture classes; "
            "hold_context_or_overlap for title/article/gender/spanish overlaps."
        ),
    }
    summary["single_operational_recommendation"] = recommendation(summary)
    return rows, summary, markdown(summary, rows)


def write(rows: list[dict[str, Any]], summary: dict[str, Any], md: str) -> dict[str, str]:
    base = reports_dir() / f"{stamp()}_narrative_faith_culture_getter_role_review_readonly"
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
    print(f"faith_culture_class_counts={json.dumps(summary['faith_culture_class_counts'], ensure_ascii=False)}")
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
