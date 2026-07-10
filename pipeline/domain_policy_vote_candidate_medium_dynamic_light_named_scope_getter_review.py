from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_medium_dynamic_light_named_scope_getter_review"
INPUT_PATH = Path("reports/20260629_135454_336991_domain_policy_vote_candidate_medium_dynamic_light_architecture_packet.jsonl")
TARGET_FAMILY = "named_scope_getters"

TITLE_RE = re.compile(r"(?:title|realm|kingdom|duchy|county|barony|empire|duke|king|emperor|liege|vassal|PRIMARY_TITLE|LandedTitle)", re.IGNORECASE)
CULTURE_RE = re.compile(r"(?:culture|tradition|innovation|heritage|ethos)", re.IGNORECASE)
FAITH_RE = re.compile(r"(?:faith|religion|doctrine|tenet|holy|piety|god|adherent)", re.IGNORECASE)
ADJECTIVE_RE = re.compile(r"(?:GetAdjective|_adj\b|adjective|demonym|plural_no_tooltip_lowercase)", re.IGNORECASE)
POSSESSIVE_RE = re.compile(r"(?:GetHerHis|GetFirstNamePossessive|GetPossessive|possessive|relation|liege|vassal|spouse|father|mother|child)", re.IGNORECASE)
PERSPECTIVE_RE = re.compile(r"Get(?:HerHim|SheHe|WomanMan|GirlBoy|HerselfHimself|HerHis|HerHisMy|HerHisTheir)\b", re.IGNORECASE)
ENTITY_NAME_RE = re.compile(r"(?:GetName|GetFullName|GetFirstName|GetTitledFirstName|CHARACTER|TARGET_CHARACTER|RECIPIENT|ACTOR)", re.IGNORECASE)


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def classify_grammar_role(row: dict[str, Any]) -> tuple[str, list[str]]:
    blob = "\n".join(
        str(row.get(key) or "")
        for key in ("english_text", "spanish_text", "current_output_text", "output_token", "source_key", "relative_path")
    )
    tags: list[str] = []
    if PERSPECTIVE_RE.search(blob):
        tags.append("perspective_pronoun_surface")
        return "perspective_pronoun", tags
    if POSSESSIVE_RE.search(blob):
        tags.append("possessive_or_relation_surface")
        return "possessive_or_relation", tags
    if TITLE_RE.search(blob):
        tags.append("title_or_realm_surface")
        return "title_or_realm_name", tags
    if CULTURE_RE.search(blob):
        tags.append("culture_surface")
        return "culture_name", tags
    if FAITH_RE.search(blob):
        tags.append("faith_surface")
        return "faith_name", tags
    if ADJECTIVE_RE.search(blob):
        tags.append("adjective_or_demonym_surface")
        return "adjective_or_demonym", tags
    if ENTITY_NAME_RE.search(blob):
        tags.append("entity_name_surface")
        return "entity_name_no_article", tags
    return "unknown_needs_context", ["no_clear_grammar_role_signal"]


def recommendation_for_role(role: str) -> str:
    if role in {"entity_name_no_article", "title_or_realm_name", "culture_name", "faith_name"}:
        return "candidate for read-only splitter policy after architecture defines token role extraction and article constraints"
    if role in {"adjective_or_demonym", "possessive_or_relation", "perspective_pronoun"}:
        return "needs parser-backed grammar policy and likely human/context validation before any automation"
    return "needs human/context packet or deeper source parser before policy"


def top_counter(counter: Counter[str], limit: int = 50) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in counter.most_common(limit)]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def write_txt(path: Path, summary: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    lines = [
        "domain_policy_vote_candidate medium_dynamic_light named_scope_getters review",
        "",
        f"input_path: {summary['input_path']}",
        f"review_count: {summary['review_count']}",
        f"expected_count: {summary['expected_count']}",
        f"count_matches_expected: {summary['count_matches_expected']}",
        "",
        "grammar_role_counts:",
    ]
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["grammar_role_counts"])
    lines.extend(["", "surface_role_counts:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["surface_role_counts"])
    lines.extend(["", "items:"])
    for row in rows:
        lines.extend(
            [
                "",
                f"## segment_id {row['segment_id']} | {row['grammar_role']} | {row.get('surface_bucket')}",
                f"- output_token: {row.get('output_token')}",
                f"- source_key: {row.get('source_key')}",
                f"- relative_path: {row.get('relative_path')}",
                f"- grammar_tags: {', '.join(row.get('grammar_tags') or [])}",
                f"- recommendation: {row.get('role_recommendation')}",
                f"- english_text: {row.get('english_text')}",
                f"- spanish_text: {row.get('spanish_text')}",
                f"- current_output_text: {row.get('current_output_text')}",
            ]
        )
    lines.extend(
        [
            "",
            "overall_recommendation:",
            summary["overall_recommendation"],
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
    input_rows = read_jsonl(INPUT_PATH)
    rows: list[dict[str, Any]] = []
    for row in input_rows:
        if row.get("architecture_family") != TARGET_FAMILY:
            continue
        role, tags = classify_grammar_role(row)
        rows.append(
            {
                **row,
                "grammar_role": role,
                "grammar_tags": tags,
                "role_recommendation": recommendation_for_role(role),
                "review_source": SOURCE,
            }
        )
    rows.sort(key=lambda row: (str(row["grammar_role"]), str(row.get("surface_bucket") or ""), int(row["segment_id"])))

    role_counts = Counter(str(row["grammar_role"]) for row in rows)
    surface_role_counts = Counter(f"{row.get('surface_bucket')} | {row.get('grammar_role')}" for row in rows)
    expected_count = 42
    overall = (
        "Create a read-only splitter/policy design for entity/title/culture/faith name getters only; keep "
        "possessive, relation, adjective/demonym, perspective, and unknown roles in architecture/human-context hold."
    )
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "read_only_named_scope_getter_review",
        "input_path": str(INPUT_PATH),
        "lane": "domain_policy_vote_candidate",
        "risk_bucket": "medium_dynamic_light",
        "architecture_family": TARGET_FAMILY,
        "expected_count": expected_count,
        "review_count": len(rows),
        "count_matches_expected": len(rows) == expected_count,
        "grammar_role_counts": top_counter(role_counts),
        "surface_role_counts": top_counter(surface_role_counts),
        "overall_recommendation": overall,
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
    write_txt(txt_path, summary, rows)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
