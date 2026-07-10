from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_medium_dynamic_light_root_getter_review"
INPUT_PATH = Path("reports/20260629_135454_336991_domain_policy_vote_candidate_medium_dynamic_light_architecture_packet.jsonl")
NAMED_SCOPE_REVIEW_PATH = Path("reports/20260629_140956_340120_domain_policy_vote_candidate_medium_dynamic_light_named_scope_getter_review.jsonl")
TARGET_FAMILY = "root_faith_culture_title_getters"
EXPECTED_COUNT = 27

DIVINE_REALM_RE = re.compile(r"(?:divine_realm|afterlife|heaven|hell|realm|god|deity|divine|holy_site|sacred)", re.IGNORECASE)
FAITH_POSSESSIVE_RE = re.compile(r"(?:faith.*possessive|religion.*possessive|_possessive|GetFaith.*Possessive|Faith.*GetNamePossessive)", re.IGNORECASE)
FAITH_ADJ_RE = re.compile(r"(?:faith.*adj|religion.*adj|GetAdjective|_adj\b|adjective)", re.IGNORECASE)
FAITH_NAME_RE = re.compile(r"(?:faith|religion|doctrine|tenet|adherent|piety|GetFaith|Faith\.GetName)", re.IGNORECASE)
CULTURE_COLLECTIVE_RE = re.compile(r"(?:culture.*collective|collective|people|folk|heritage|tradition.*plural|adherent_plural)", re.IGNORECASE)
CULTURE_NAME_RE = re.compile(r"(?:culture|tradition|innovation|heritage|ethos|GetCulture|Culture\.GetName)", re.IGNORECASE)
TITLE_BASE_RE = re.compile(r"(?:PRIMARY_TITLE_BASE_NAME|SPOUSE_PRIMARY_TITLE_BASE_NAME|BaseName|GetBaseName|title_base)", re.IGNORECASE)
TITLE_REALM_RE = re.compile(r"(?:title|realm|kingdom|duchy|county|barony|empire|duke|king|emperor|liege|vassal|LandedTitle|PrimaryTitle)", re.IGNORECASE)


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


def classify_grammar_role(row: dict[str, Any]) -> tuple[str, list[str]]:
    text = blob(row)
    tags: list[str] = []
    if TITLE_BASE_RE.search(text):
        tags.append("title_base_surface")
        return "title_base_name", tags
    if FAITH_POSSESSIVE_RE.search(text):
        tags.append("faith_possessive_surface")
        return "faith_possessive", tags
    if FAITH_ADJ_RE.search(text):
        tags.append("faith_adjective_surface")
        return "faith_adjective", tags
    if DIVINE_REALM_RE.search(text) and not TITLE_REALM_RE.search(text):
        tags.append("divine_realm_or_concept_surface")
        return "divine_realm_or_concept", tags
    if CULTURE_COLLECTIVE_RE.search(text):
        tags.append("culture_collective_surface")
        return "culture_collective", tags
    if CULTURE_NAME_RE.search(text):
        tags.append("culture_name_surface")
        return "culture_name", tags
    if TITLE_REALM_RE.search(text):
        tags.append("title_or_realm_surface")
        return "title_or_realm_name", tags
    if FAITH_NAME_RE.search(text):
        tags.append("faith_name_surface")
        return "faith_name", tags
    return "unknown_needs_context", ["no_clear_root_getter_role_signal"]


def top_counter(counter: Counter[str], limit: int = 50) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in counter.most_common(limit)]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def combined_recommendation(root_counts: Counter[str], named_counts: Counter[str]) -> dict[str, Any]:
    combined = Counter(named_counts)
    combined.update(root_counts)
    hold_roles = {
        "possessive_or_relation",
        "faith_possessive",
        "faith_adjective",
        "culture_collective",
        "divine_realm_or_concept",
        "unknown_needs_context",
    }
    splitter_roles = {
        "faith_name",
        "culture_name",
        "title_or_realm_name",
        "title_base_name",
    }
    return {
        "combined_role_counts": top_counter(combined),
        "recommended_unified_splitter_roles": sorted(role for role in splitter_roles if combined.get(role, 0) > 0),
        "recommended_hold_roles": sorted(role for role in hold_roles if combined.get(role, 0) > 0),
        "policy_recommendation": (
            "Design one unified medium_dynamic_light getter splitter that consumes named-scope and root getter families, "
            "routes faith_name/title_or_realm_name/culture_name/title_base_name read-only, and keeps possessive, adjective, "
            "collective, divine concept, and unknown roles in hold until parser-backed grammar policy exists."
        ),
    }


def write_txt(path: Path, summary: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    lines = [
        "domain_policy_vote_candidate medium_dynamic_light root_faith_culture_title_getters review",
        "",
        f"input_path: {summary['input_path']}",
        f"review_count: {summary['review_count']}",
        f"expected_count: {summary['expected_count']}",
        f"count_matches_expected: {str(summary['count_matches_expected']).lower()}",
        "",
        "grammar_role_counts:",
    ]
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["grammar_role_counts"])
    lines.extend(["", "surface_role_counts:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["surface_role_counts"])
    lines.extend(["", "combined_named_scope_plus_root_role_counts:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["combined_recommendation"]["combined_role_counts"])
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
                f"- english_text: {row.get('english_text')}",
                f"- spanish_text: {row.get('spanish_text')}",
                f"- current_output_text: {row.get('current_output_text')}",
            ]
        )
    lines.extend(
        [
            "",
            "recommendation:",
            summary["combined_recommendation"]["policy_recommendation"],
            "",
            "recommended_unified_splitter_roles:",
        ]
    )
    lines.extend(f"- {role}" for role in summary["combined_recommendation"]["recommended_unified_splitter_roles"])
    lines.extend(["", "recommended_hold_roles:"])
    lines.extend(f"- {role}" for role in summary["combined_recommendation"]["recommended_hold_roles"])
    lines.extend(
        [
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
                "review_source": SOURCE,
                "role_recommendation": "read_only_splitter_candidate" if role in {"faith_name", "culture_name", "title_or_realm_name", "title_base_name"} else "hold_for_parser_or_context",
            }
        )
    rows.sort(key=lambda row: (str(row["grammar_role"]), str(row.get("surface_bucket") or ""), int(row["segment_id"])))

    if len(rows) != EXPECTED_COUNT:
        raise SystemExit(f"review count guard failed: {len(rows)} expected {EXPECTED_COUNT}")

    root_counts = Counter(str(row["grammar_role"]) for row in rows)
    surface_role_counts = Counter(f"{row.get('surface_bucket')} | {row.get('grammar_role')}" for row in rows)
    named_rows = read_jsonl(NAMED_SCOPE_REVIEW_PATH)
    named_counts = Counter(str(row.get("grammar_role") or "") for row in named_rows)
    combined = combined_recommendation(root_counts, named_counts)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "read_only_root_getter_review",
        "input_path": str(INPUT_PATH),
        "named_scope_review_path": str(NAMED_SCOPE_REVIEW_PATH),
        "lane": "domain_policy_vote_candidate",
        "risk_bucket": "medium_dynamic_light",
        "architecture_family": TARGET_FAMILY,
        "expected_count": EXPECTED_COUNT,
        "review_count": len(rows),
        "count_matches_expected": len(rows) == EXPECTED_COUNT,
        "grammar_role_counts": top_counter(root_counts),
        "surface_role_counts": top_counter(surface_role_counts),
        "named_scope_role_counts": top_counter(named_counts),
        "combined_recommendation": combined,
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
