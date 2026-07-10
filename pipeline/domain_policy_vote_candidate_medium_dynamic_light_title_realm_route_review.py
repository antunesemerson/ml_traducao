from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_medium_dynamic_light_title_realm_route_review"
INPUT_PATH = Path("reports/20260629_145447_528523_medium_dynamic_light_getter_role_policy_dry_run.jsonl")
TARGET_ROUTE = "route_getter_title_or_realm_name"
EXPECTED_COUNT = 21

BASE_NAME_RE = re.compile(r"(?:BaseName|PRIMARY_TITLE_BASE_NAME|SPOUSE_PRIMARY_TITLE_BASE_NAME|base_name)", re.IGNORECASE)
REALM_RE = re.compile(r"(?:realm|kingdom|empire|duchy|county|barony|dom[ií]nio|reino|imp[eé]rio|ducado|condado)", re.IGNORECASE)
TITLE_RE = re.compile(r"(?:title|LandedTitle|PrimaryTitle|t[ií]tulo|duke|king|emperor|duque|rei|imperador|ruler)", re.IGNORECASE)
ARTICLE_PREP_RE = re.compile(r"\b(?:of|from|in|de|do|da|dos|das|no|na|nos|nas|ao|à|aos|às)\b|[\"']s\b", re.IGNORECASE)
GENDER_CASE_RE = re.compile(r"\b(?:male|female|masculin|feminin|gender|GetHerHis|GetSheHe|GetHerHim|do/da|o/a|um/uma)\b", re.IGNORECASE)


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
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
        for key in ("english_text", "spanish_text", "current_output_text", "output_token", "source_key", "relative_path")
    )


def classify_subtype(row: dict[str, Any]) -> tuple[str, list[str]]:
    text = blob(row)
    tags: list[str] = []
    if BASE_NAME_RE.search(text):
        tags.append("base_name_surface")
        return "landed_title_base_name", tags
    if GENDER_CASE_RE.search(text):
        tags.append("case_or_gender_surface")
        return "requires_case_or_gender_context", tags
    if ARTICLE_PREP_RE.search(text):
        tags.append("article_or_preposition_surface")
        return "requires_article_or_preposition_context", tags
    if REALM_RE.search(text):
        tags.append("realm_name_surface")
        return "realm_name_preserve_as_proper_name", tags
    if TITLE_RE.search(text):
        tags.append("title_name_surface")
        return "title_name_preserve_as_proper_name", tags
    return "hold_context_uncertain", ["no_clear_title_realm_subtype_signal"]


def representative_samples(rows: list[dict[str, Any]], per_subtype: int = 4) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["operational_subtype"])].append(row)
    sample: list[dict[str, Any]] = []
    for subtype in sorted(grouped):
        sample.extend(grouped[subtype][:per_subtype])
    return sample


def top_counter(counter: Counter[str], limit: int = 50) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in counter.most_common(limit)]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def write_txt(path: Path, summary: dict[str, Any], rows: list[dict[str, Any]], sample: list[dict[str, Any]]) -> None:
    lines = [
        "domain_policy_vote_candidate medium_dynamic_light title_or_realm route review",
        "",
        f"input_path: {summary['input_path']}",
        f"policy_parent: {summary['policy_parent']}",
        f"target_route: {summary['target_route']}",
        f"review_count: {summary['review_count']}",
        f"expected_count: {summary['expected_count']}",
        f"count_matches_expected: {str(summary['count_matches_expected']).lower()}",
        "",
        "subtype_counts:",
    ]
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["subtype_counts"])
    lines.extend(["", "source_family_subtype_counts:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["source_family_subtype_counts"])
    lines.extend(["", "representative_examples:"])
    for row in sample:
        lines.extend(
            [
                "",
                f"## segment_id {row['segment_id']} | {row['operational_subtype']} | {row.get('source_family')}",
                f"- output_token: {row.get('output_token')}",
                f"- source_key: {row.get('source_key')}",
                f"- relative_path: {row.get('relative_path')}",
                f"- subtype_tags: {', '.join(row.get('subtype_tags') or [])}",
                f"- english_text: {row.get('english_text')}",
                f"- spanish_text: {row.get('spanish_text')}",
                f"- current_output_text: {row.get('current_output_text')}",
            ]
        )
    lines.extend(
        [
            "",
            "recommendation:",
            summary["recommendation"],
            "",
            "gates:",
            "- candidate_generation: not_run",
            "- apply: not_run",
            "- lifecycle: not_run",
            "- segment_state: not_run",
            "- reindex: not_run",
            "- full_production: not_run",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    input_rows = [row for row in read_jsonl(INPUT_PATH) if row.get("route") == TARGET_ROUTE]
    if len(input_rows) != EXPECTED_COUNT:
        raise SystemExit(f"route count guard failed: {len(input_rows)} expected {EXPECTED_COUNT}")

    rows: list[dict[str, Any]] = []
    for row in input_rows:
        subtype, tags = classify_subtype(row)
        rows.append(
            {
                **row,
                "operational_subtype": subtype,
                "subtype_tags": tags,
                "review_source": SOURCE,
                "subtype_recommendation": (
                    "child_splitter_read_only_candidate"
                    if subtype in {"title_name_preserve_as_proper_name", "realm_name_preserve_as_proper_name", "landed_title_base_name"}
                    else "hold_for_human_or_parser_context"
                ),
            }
        )
    rows.sort(key=lambda row: (str(row["operational_subtype"]), str(row.get("source_family") or ""), int(row["segment_id"])))

    subtype_counts = Counter(str(row["operational_subtype"]) for row in rows)
    source_subtype_counts = Counter(f"{row.get('source_family')} | {row.get('operational_subtype')}" for row in rows)
    candidate_count = sum(
        subtype_counts[subtype]
        for subtype in ("title_name_preserve_as_proper_name", "realm_name_preserve_as_proper_name", "landed_title_base_name")
    )
    hold_count = len(rows) - candidate_count
    recommendation = (
        "Register a child read-only splitter only if architecture accepts preserving proper-name title/realm/base-name surfaces; "
        "items requiring article/preposition or case/gender context should remain held and should not generate candidates."
    )
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "read_only_route_review",
        "input_path": str(INPUT_PATH),
        "policy_parent": "medium_dynamic_light_getter_role_policy",
        "lane": "domain_policy_vote_candidate",
        "risk_bucket": "medium_dynamic_light",
        "target_route": TARGET_ROUTE,
        "expected_count": EXPECTED_COUNT,
        "review_count": len(rows),
        "count_matches_expected": len(rows) == EXPECTED_COUNT,
        "subtype_counts": top_counter(subtype_counts),
        "source_family_subtype_counts": top_counter(source_subtype_counts),
        "child_splitter_candidate_count": candidate_count,
        "hold_or_context_count": hold_count,
        "representative_sample_count": len(representative_samples(rows)),
        "recommendation": recommendation,
        "gates": {
            "candidate_generation": "not_run",
            "apply": "not_run",
            "lifecycle": "not_run",
            "segment_state": "not_run",
            "reindex": "not_run",
            "full_production": "not_run",
        },
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "output_files": {},
    }
    sample = representative_samples(rows)
    base = reports_dir() / f"{stamp()}_{SOURCE}"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    write_jsonl(jsonl_path, rows)
    summary["output_files"] = {
        "txt": str(txt_path),
        "jsonl": str(jsonl_path),
        "summary_json": str(summary_path),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_txt(txt_path, summary, rows, sample)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
