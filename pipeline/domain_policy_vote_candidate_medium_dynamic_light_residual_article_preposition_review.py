from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_medium_dynamic_light_residual_article_preposition_review"
INPUT_PATH = Path("reports/20260629_203140_344770_medium_dynamic_light_residual_parser_policy_dry_run.jsonl")
TARGET_ROUTE = "route_residual_article_preposition_title_faith_culture"
EXPECTED_COUNT = 21

TITLE_REALM_RE = re.compile(
    r"(?:landed_title|kingdom|realm|title|County|SubRegion|GetBuilding|building_|nickname|epithet|liege|defender|mandala|ruler)",
    re.IGNORECASE,
)
FAITH_CULTURE_RE = re.compile(r"(?:Faith|faith|culture|Culture|HighGod|temple|religious|cultura|fé|fe)", re.IGNORECASE)
ARTICLE_BEFORE_TOKEN_RE = re.compile(r"\b(?:o|a|os|as|um|uma|the|el|la|los|las)\s+\[[^\]]+\]", re.IGNORECASE)
PREPOSITION_BEFORE_TOKEN_RE = re.compile(r"\b(?:de|do|da|dos|das|em|no|na|nos|nas|a|ao|à|aos|às|para|por|from|of|in|to)\s+\[[^\]]+\]", re.IGNORECASE)
CONTRACTION_RE = re.compile(r"\b(?:do|da|dos|das|no|na|nos|nas|ao|à|aos|às|del|al)\s+\[[^\]]+\]", re.IGNORECASE)
NO_ARTICLE_PATTERNS = re.compile(r"(?:Localização:\s*\[|Ubicación:\s*\[|Possuir .* para \[|sinal de \[|testemunho a \[|sinal de \[)", re.IGNORECASE)


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


def text_blob(row: dict[str, Any]) -> str:
    return "\n".join(
        str(row.get(key) or "")
        for key in ("english_text", "spanish_text", "current_output_text", "source_key", "relative_path", "dynamic_tokens")
    )


def token_text(row: dict[str, Any]) -> str:
    tokens = row.get("dynamic_tokens") or []
    if isinstance(tokens, list):
        return " ".join(str(token) for token in tokens)
    return str(tokens or "")


def domain(row: dict[str, Any]) -> str:
    text = text_blob(row)
    token = token_text(row)
    if FAITH_CULTURE_RE.search(text) or FAITH_CULTURE_RE.search(token):
        return "faith_or_culture"
    if TITLE_REALM_RE.search(text) or TITLE_REALM_RE.search(token):
        return "title_or_realm"
    return "unknown_domain"


def classify_subtype(row: dict[str, Any]) -> tuple[str, list[str], str]:
    text = text_blob(row)
    current = str(row.get("current_output_text") or "")
    dom = domain(row)
    tags: list[str] = [dom]

    if CONTRACTION_RE.search(current):
        tags.append("contraction_surface")
        return (
            "contraction_required_de_da_do_dos_das",
            tags,
            "Contraction is already present or required; needs parser to know token article/gender.",
        )
    if ARTICLE_BEFORE_TOKEN_RE.search(current):
        tags.append("article_before_token")
        if dom == "faith_or_culture":
            return ("article_before_faith_or_culture", tags, "Article before faith/culture getter; parser can classify but should not auto-generate yet.")
        if dom == "title_or_realm":
            return ("article_before_title_or_realm", tags, "Article before title/realm getter; parser can classify but should not auto-generate yet.")
    if PREPOSITION_BEFORE_TOKEN_RE.search(current):
        tags.append("preposition_before_token")
        if dom == "faith_or_culture":
            return ("preposition_before_faith_or_culture", tags, "Preposition before faith/culture getter; needs article/contractability metadata.")
        if dom == "title_or_realm":
            return ("preposition_before_title_or_realm", tags, "Preposition before title/realm getter; needs article/contractability metadata.")
    if NO_ARTICLE_PATTERNS.search(current):
        tags.append("no_article_pattern")
        return ("no_article_proper_name", tags, "Looks intentionally article-free around a proper name/getter.")
    return ("uncertain_needs_context", tags + ["no_specific_article_preposition_signal"], "Needs human/context review before parser policy.")


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
        "domain_policy_vote_candidate medium_dynamic_light residual article/preposition route review",
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
    lines.extend(["", "surface_subtype_counts:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["surface_subtype_counts"])
    lines.extend(["", "representative_examples:"])
    for row in sample:
        lines.extend(
            [
                "",
                f"## segment_id {row['segment_id']} | {row['operational_subtype']}",
                f"- surface_bucket: {row.get('surface_bucket')}",
                f"- source_key: {row.get('source_key')}",
                f"- dynamic_tokens: {', '.join(row.get('dynamic_tokens') or [])}",
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
    input_rows = [row for row in read_jsonl(INPUT_PATH) if row.get("route") == TARGET_ROUTE]
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
    rows.sort(key=lambda row: (str(row["operational_subtype"]), str(row.get("surface_bucket") or ""), int(row["segment_id"])))

    subtype_counts = Counter(str(row["operational_subtype"]) for row in rows)
    surface_subtype_counts = Counter(f"{row.get('surface_bucket')} | {row.get('operational_subtype')}" for row in rows)
    policy_candidate_subtypes = {
        "article_before_title_or_realm",
        "preposition_before_title_or_realm",
        "article_before_faith_or_culture",
        "preposition_before_faith_or_culture",
        "contraction_required_de_da_do_dos_das",
        "no_article_proper_name",
    }
    parser_policy_candidate_count = sum(subtype_counts[subtype] for subtype in policy_candidate_subtypes)
    hold_or_human_count = len(rows) - parser_policy_candidate_count
    recommendation = (
        "This route is suitable for a read-only parser/splitter, not for candidate generation. "
        "Architecture should first materialize subtype detection plus token article/gender/contractability metadata; "
        "uncertain rows should remain human/context hold."
    )
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "read_only_route_review",
        "input_path": str(INPUT_PATH),
        "policy_parent": "medium_dynamic_light_residual_parser_policy",
        "lane": "domain_policy_vote_candidate",
        "risk_bucket": "medium_dynamic_light",
        "target_route": TARGET_ROUTE,
        "expected_count": EXPECTED_COUNT,
        "review_count": len(rows),
        "count_matches_expected": len(rows) == EXPECTED_COUNT,
        "subtype_counts": top_counter(subtype_counts),
        "surface_subtype_counts": top_counter(surface_subtype_counts),
        "parser_policy_candidate_count": parser_policy_candidate_count,
        "hold_or_human_count": hold_or_human_count,
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
