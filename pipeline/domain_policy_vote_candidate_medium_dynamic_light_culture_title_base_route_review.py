from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_medium_dynamic_light_culture_title_base_route_review"
INPUT_PATH = Path("reports/20260629_145447_528523_medium_dynamic_light_getter_role_policy_dry_run.jsonl")
TARGET_ROUTES = {"route_getter_culture_name", "route_getter_title_base_name"}
EXPECTED_COUNT = 9

CULTURE_NAME_RE = re.compile(r"(?:Culture\.GetName|GetCulture\.GetName|GetNameNoTooltip|sicilian|goryeo)", re.IGNORECASE)
ADHERENT_RE = re.compile(r"(?:AdherentName|faith)", re.IGNORECASE)
TITLE_BASE_RE = re.compile(r"(?:Title\.GetBaseName|PRIMARY_TITLE_BASE_NAME|base_name)", re.IGNORECASE)
ARTICLE_PREP_RE = re.compile(r"\b(?:of|de|do|da|dos|das|the|o|a|os|as|ganhou|perdeu|lost|gained|seized|tomou|pastagens)\b", re.IGNORECASE)


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


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def blob(row: dict[str, Any]) -> str:
    return "\n".join(
        str(row.get(key) or "")
        for key in ("english_text", "spanish_text", "current_output_text", "output_token", "source_key", "relative_path")
    )


def classify_subtype(row: dict[str, Any]) -> tuple[str, list[str], str]:
    text = blob(row)
    token = str(row.get("output_token") or "")
    route = str(row.get("route") or "")
    tags: list[str] = []

    if route == "route_getter_culture_name":
        if ADHERENT_RE.search(token):
            tags.append("faith_adherent_token_misrouted_as_culture")
            return (
                "misrouted_faith_adherent_plural",
                tags,
                "hold_context; this is not a culture-name rule and should stay out of culture splitter",
            )
        if CULTURE_NAME_RE.search(text):
            tags.append("culture_name_token")
            if ARTICLE_PREP_RE.search(text):
                tags.append("culture_article_preposition_surface")
                return (
                    "culture_name_article_preposition_context",
                    tags,
                    "safe for human confirmation; do not candidate-generate until article/preposition policy exists",
                )
            return (
                "culture_name_preserve_as_proper_name",
                tags,
                "possible read-only splitter child; preserve culture-name token without candidate generation",
            )
        return ("hold_context_uncertain", ["no_clear_culture_signal"], "hold_context; requires manual/context review")

    if route == "route_getter_title_base_name":
        if TITLE_BASE_RE.search(token):
            tags.append("title_base_name_token")
            if ARTICLE_PREP_RE.search(text):
                tags.append("title_base_article_preposition_surface")
                return (
                    "title_base_article_preposition_context",
                    tags,
                    "hold_context; base title expansion may require article/preposition/case handling",
                )
            return (
                "title_base_preserve_as_proper_name",
                tags,
                "possible read-only splitter child; preserve title-base token without candidate generation",
            )
        return ("hold_context_uncertain", ["no_clear_title_base_signal"], "hold_context; requires manual/context review")

    return ("hold_context_uncertain", ["unexpected_route"], "hold_context; unexpected route")


def top_counter(counter: Counter[str], limit: int = 50) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in counter.most_common(limit)]


def representative_samples(rows: list[dict[str, Any]], per_subtype: int = 4) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["operational_subtype"])].append(row)
    sample: list[dict[str, Any]] = []
    for subtype in sorted(grouped):
        sample.extend(grouped[subtype][:per_subtype])
    return sample


def write_txt(path: Path, summary: dict[str, Any], sample: list[dict[str, Any]]) -> None:
    lines = [
        "domain_policy_vote_candidate medium_dynamic_light culture/title-base route review",
        "",
        f"input_path: {summary['input_path']}",
        f"policy_parent: {summary['policy_parent']}",
        f"target_routes: {', '.join(summary['target_routes'])}",
        f"review_count: {summary['review_count']}",
        f"expected_count: {summary['expected_count']}",
        f"count_matches_expected: {str(summary['count_matches_expected']).lower()}",
        "",
        "route_counts:",
    ]
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["route_counts"])
    lines.extend(["", "subtype_counts:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["subtype_counts"])
    lines.extend(["", "route_subtype_counts:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["route_subtype_counts"])
    lines.extend(["", "representative_examples:"])
    for row in sample:
        lines.extend(
            [
                "",
                f"## segment_id {row['segment_id']} | {row['route']} | {row['operational_subtype']}",
                f"- output_token: {row.get('output_token')}",
                f"- source_key: {row.get('source_key')}",
                f"- relative_path: {row.get('relative_path')}",
                f"- subtype_tags: {', '.join(row.get('subtype_tags') or [])}",
                f"- subtype_recommendation: {row.get('subtype_recommendation')}",
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
    input_rows = [row for row in read_jsonl(INPUT_PATH) if row.get("route") in TARGET_ROUTES]
    if len(input_rows) != EXPECTED_COUNT:
        raise SystemExit(f"route count guard failed: {len(input_rows)} expected {EXPECTED_COUNT}")

    rows: list[dict[str, Any]] = []
    for row in input_rows:
        subtype, tags, subtype_recommendation = classify_subtype(row)
        rows.append(
            {
                **row,
                "operational_subtype": subtype,
                "subtype_tags": tags,
                "subtype_recommendation": subtype_recommendation,
                "review_source": SOURCE,
            }
        )
    rows.sort(key=lambda row: (str(row["route"]), str(row["operational_subtype"]), int(row["segment_id"])))

    route_counts = Counter(str(row["route"]) for row in rows)
    subtype_counts = Counter(str(row["operational_subtype"]) for row in rows)
    route_subtype_counts = Counter(f"{row.get('route')} | {row.get('operational_subtype')}" for row in rows)
    child_splitter_candidate_count = sum(
        subtype_counts[subtype]
        for subtype in ("culture_name_preserve_as_proper_name", "title_base_preserve_as_proper_name")
    )
    hold_or_context_count = len(rows) - child_splitter_candidate_count
    recommendation = (
        "Do not register child splitters yet. Culture-name items are mostly safe but still article/preposition shaped, "
        "and one item is a faith-adherent plural misroute. Title-base items all require article/preposition/context. "
        "The best next step is a tiny human review for these 9 rows, then feed confirmed signals back into learning."
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
        "target_routes": sorted(TARGET_ROUTES),
        "expected_count": EXPECTED_COUNT,
        "review_count": len(rows),
        "count_matches_expected": len(rows) == EXPECTED_COUNT,
        "route_counts": top_counter(route_counts),
        "subtype_counts": top_counter(subtype_counts),
        "route_subtype_counts": top_counter(route_subtype_counts),
        "child_splitter_candidate_count": child_splitter_candidate_count,
        "hold_or_context_count": hold_or_context_count,
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
    write_txt(txt_path, summary, sample)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
