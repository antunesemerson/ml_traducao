from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_medium_dynamic_light_residual_generic_getter_review"
INPUT_PATH = Path("reports/20260629_203140_344770_medium_dynamic_light_residual_parser_policy_dry_run.jsonl")
TARGET_ROUTE = "route_residual_generic_getter_role_unknown"
EXPECTED_COUNT = 26

FAITH_RE = re.compile(r"(?:ReligionFamily|religions?|faith|holy|piety|church|monastery|cathedral|pilgrim|GetBuilding\('holy_|GetBuilding\('mont_st_michel|notre_dame)", re.IGNORECASE)
TITLE_REALM_RE = re.compile(r"(?:kingdom|realm|title|county|liege|vassal|confederation|GetNameNoTier|title\.GetName|target\.GetName|GetModifier|appointment)", re.IGNORECASE)
CULTURE_RE = re.compile(r"(?:culture|tradition|imperial influence|GetBuilding\('hakata_port)", re.IGNORECASE)
CHARACTER_RE = re.compile(r"(?:GetFirstName|ShortUIName|CHARACTER\.Self|TRAIT\.GetName|liege\.GetName|vassal|trait)", re.IGNORECASE)
PLACE_RE = re.compile(r"(?:GetBuilding|County\.GetSubRegion|island|cathedral|basilica|monasteries|destination|port|mount|notre_dame)", re.IGNORECASE)
RELATION_RE = re.compile(r"(?:RelationToMe|Custom2\('RelationToMe|Possessive|GetHerHis|GetSheHe|GetHerHim)", re.IGNORECASE)
ARTICLE_PREP_CONTEXT_RE = re.compile(r"\b(?:o|a|os|as|um|uma|de|do|da|dos|das|em|no|na|para|a|ao|à|of|the|to|in)\s+\[[^\]]+\]", re.IGNORECASE)


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
    tokens = row.get("dynamic_tokens") or []
    return "\n".join(
        str(row.get(key) or "")
        for key in ("english_text", "spanish_text", "current_output_text", "source_key", "relative_path", "surface_bucket")
    ) + "\n" + " ".join(str(token) for token in tokens)


def classify_role(row: dict[str, Any]) -> tuple[str, list[str], str, str]:
    text = blob(row)
    current = str(row.get("current_output_text") or "")
    tags: list[str] = []

    if RELATION_RE.search(text):
        tags.append("relation_or_possessive_surface")
        return (
            "relation_or_possessive",
            tags,
            "hold",
            "Keep in hold or send to perspective/relation parser; not article/preposition policy.",
        )
    if ARTICLE_PREP_CONTEXT_RE.search(current):
        tags.append("article_preposition_surface")
        return (
            "article_preposition_context",
            tags,
            "article_preposition_policy",
            "Absorb into medium_dynamic_light_article_preposition_policy after subtype review.",
        )
    if PLACE_RE.search(text):
        tags.append("place_or_building_surface")
        return (
            "place_or_barony_name",
            tags,
            "getter_role_policy",
            "Absorb into getter-role policy as place/building/proper-name route, with article metadata later.",
        )
    if FAITH_RE.search(text):
        tags.append("faith_surface")
        return (
            "faith_name",
            tags,
            "getter_role_policy",
            "Absorb into getter-role policy as faith/religion-family route.",
        )
    if CULTURE_RE.search(text):
        tags.append("culture_surface")
        return (
            "culture_name",
            tags,
            "getter_role_policy",
            "Absorb into getter-role policy as culture/building-culture-adjacent route.",
        )
    if TITLE_REALM_RE.search(text):
        tags.append("title_realm_surface")
        return (
            "title_or_realm_name",
            tags,
            "getter_role_policy",
            "Absorb into getter-role policy as title/realm/modifier route.",
        )
    if CHARACTER_RE.search(text):
        tags.append("character_or_trait_surface")
        return (
            "character_or_actor_name",
            tags,
            "getter_role_policy",
            "Absorb into getter-role policy as character/trait label route.",
        )
    return (
        "hold_unknown",
        ["no_clear_generic_getter_role"],
        "hold",
        "Keep in hold until domain context is reviewed.",
    )


def top_counter(counter: Counter[str], limit: int = 50) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in counter.most_common(limit)]


def representative_samples(rows: list[dict[str, Any]], per_role: int = 4) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["grammar_role_review"])].append(row)
    sample: list[dict[str, Any]] = []
    for role in sorted(grouped):
        sample.extend(grouped[role][:per_role])
    return sample


def write_txt(path: Path, summary: dict[str, Any], sample: list[dict[str, Any]]) -> None:
    lines = [
        "domain_policy_vote_candidate medium_dynamic_light residual generic getter role review",
        "",
        f"input_path: {summary['input_path']}",
        f"policy_parent: {summary['policy_parent']}",
        f"target_route: {summary['target_route']}",
        f"review_count: {summary['review_count']}",
        f"expected_count: {summary['expected_count']}",
        f"count_matches_expected: {str(summary['count_matches_expected']).lower()}",
        "",
        "grammar_role_counts:",
    ]
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["grammar_role_counts"])
    lines.extend(["", "absorption_recommendation_counts:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["absorption_recommendation_counts"])
    lines.extend(["", "surface_role_counts:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["surface_role_counts"])
    lines.extend(["", "representative_examples:"])
    for row in sample:
        lines.extend(
            [
                "",
                f"## segment_id {row['segment_id']} | {row['grammar_role_review']} | {row['absorption_recommendation']}",
                f"- surface_bucket: {row.get('surface_bucket')}",
                f"- source_key: {row.get('source_key')}",
                f"- dynamic_tokens: {', '.join(row.get('dynamic_tokens') or [])}",
                f"- role_tags: {', '.join(row.get('role_tags') or [])}",
                f"- role_recommendation: {row.get('role_recommendation')}",
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
        role, tags, absorption, role_recommendation = classify_role(row)
        rows.append(
            {
                **row,
                "grammar_role_review": role,
                "role_tags": tags,
                "absorption_recommendation": absorption,
                "role_recommendation": role_recommendation,
                "review_source": SOURCE,
            }
        )
    rows.sort(key=lambda row: (str(row["absorption_recommendation"]), str(row["grammar_role_review"]), str(row.get("surface_bucket") or ""), int(row["segment_id"])))

    role_counts = Counter(str(row["grammar_role_review"]) for row in rows)
    absorption_counts = Counter(str(row["absorption_recommendation"]) for row in rows)
    surface_role_counts = Counter(f"{row.get('surface_bucket')} | {row.get('grammar_role_review')}" for row in rows)
    recommendation = (
        "Most unknown getters should be absorbed by getter-role policy or article/preposition policy as read-only splitters; "
        "relation/possessive and hold_unknown rows must remain hold until a perspective/domain parser exists."
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
        "grammar_role_counts": top_counter(role_counts),
        "absorption_recommendation_counts": top_counter(absorption_counts),
        "surface_role_counts": top_counter(surface_role_counts),
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
