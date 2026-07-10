from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import domain_policy_vote_candidate_deep_diagnostic as deep


SOURCE = "domain_policy_vote_candidate_medium_dynamic_light_residual_architecture_packet"
SEGMENT_STATE_RUN_ID = 502
EXPECTED_RISK_BUCKET = "medium_dynamic_light"

SELECT_RE = re.compile(r"Select(?:Localization|_CString)|Select_CString", re.IGNORECASE)
PANTHEON_RE = re.compile(r"PantheonTerm", re.IGNORECASE)
CLERGY_ADHERENT_RE = re.compile(r"(?:Priest|Cleric|Clergy|AdherentName|GetAdjective)", re.IGNORECASE)
POSSESSIVE_RELATION_RE = re.compile(r"(?:GetHerHis|GetHerHim|GetSheHe|GetHerHisTheir|GetRelation|GetMotherFather|GetDaughterSon|GetWifeHusband)", re.IGNORECASE)
CULTURE_COLLECTIVE_RE = re.compile(r"(?:GetCollectiveNoun|povo \[[^\]]*Culture|people.*Culture)", re.IGNORECASE)
PERSPECTIVE_RE = re.compile(r"(?:GetHerHim|GetSheHe|GetWomanMan|GetGirlBoy|GetHerselfHimself|GetHerHis|GetHerHisMy|GetHerHisTheir)", re.IGNORECASE)
TITLE_GETTER_RE = re.compile(r"(?:Title\.GetBaseName|GetPrimaryTitle|PrimaryTitle|landed_title|GetTitle|GetNameNoTier|realm|kingdom|barony|duchy|county)", re.IGNORECASE)
FAITH_GETTER_RE = re.compile(r"(?:Faith\.GetName|GetFaith|faith\.GetName|HighGodName|PantheonTerm|faith.GetName|Faith.GetAdherent)", re.IGNORECASE)
CULTURE_GETTER_RE = re.compile(r"(?:Culture\.GetName|GetCulture|culture \[[^\]]*GetName|GetNameNoTooltip.*culture)", re.IGNORECASE)
ARTICLE_PREP_RE = re.compile(
    r"\b(?:of|in|from|de|do|da|dos|das|del|no|na|nos|nas|ao|aos|à|às|"
    r"cultura|culture|fé|faith|reino|realm|pastagens|truth|verdade)\b",
    re.IGNORECASE,
)
GETTER_RE = re.compile(r"\[[^\]]*(?:Get|SelectLocalization|Select_CString)[^\]]*\]|\[[A-Za-z_][A-Za-z0-9_]*\.[^\]]+\]")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def combined(row: dict[str, Any]) -> str:
    return "\n".join(
        str(row.get(key) or "")
        for key in ("english_text", "spanish_text", "current_output_text", "source_key", "relative_path")
    )


def tokens(row: dict[str, Any]) -> list[str]:
    text = str(row.get("current_output_text") or "")
    return deep.TOKEN_RE.findall(text)


def architecture_family(row: dict[str, Any]) -> tuple[str, str, list[str], str]:
    text = combined(row)
    token_text = " ".join(tokens(row))
    tags: list[str] = []

    if SELECT_RE.search(text):
        tags.append("select_localization_or_select_cstring")
        return (
            "select_localization_select_cstring",
            "hold_architecture",
            tags,
            "Needs selection parser; do not treat as plain token replacement.",
        )
    if PERSPECTIVE_RE.search(text):
        tags.append("perspective_getter_surface")
        return (
            "getter_perspective_omitted",
            "hold_architecture",
            tags,
            "Needs explicit perspective getter preservation/omission policy.",
        )
    if PANTHEON_RE.search(text):
        tags.append("pantheon_term")
        return (
            "pantheonterm_agreement",
            "hold_architecture",
            tags,
            "Needs singular/plural agreement policy for PantheonTerm.",
        )
    if CLERGY_ADHERENT_RE.search(text):
        tags.append("clergy_or_adherent")
        return (
            "clergy_adherent_role_agreement",
            "human_or_parser_policy",
            tags,
            "Needs role, number, and gender handling before automation.",
        )
    if CULTURE_COLLECTIVE_RE.search(text):
        tags.append("culture_collective")
        return (
            "culture_collective",
            "hold_architecture",
            tags,
            "Collective culture noun needs article/number agreement and subject role.",
        )
    if POSSESSIVE_RELATION_RE.search(text):
        tags.append("possessive_or_relation_getter")
        return (
            "possessive_or_relation",
            "hold_architecture",
            tags,
            "Possessive/relation getter needs perspective and grammatical-role parser.",
        )
    if (TITLE_GETTER_RE.search(text) or FAITH_GETTER_RE.search(text) or CULTURE_GETTER_RE.search(text)) and ARTICLE_PREP_RE.search(text):
        if TITLE_GETTER_RE.search(text):
            tags.append("title_getter")
        if FAITH_GETTER_RE.search(text):
            tags.append("faith_getter")
        if CULTURE_GETTER_RE.search(text):
            tags.append("culture_getter")
        tags.append("article_preposition_surface")
        return (
            "article_preposition_title_faith_culture",
            "parser_policy_candidate",
            tags,
            "Candidate for parser policy that classifies article/preposition around title, faith, and culture getters.",
        )
    if GETTER_RE.search(token_text) or GETTER_RE.search(text):
        tags.append("generic_getter_surface")
        return (
            "generic_getter_role_unknown",
            "human_or_parser_policy",
            tags,
            "Getter role is unclear; needs role splitter before candidate generation.",
        )
    return (
        "residual_medium_dynamic_light_human",
        "human_review",
        ["no_specific_parser_family"],
        "No specific architecture family matched; keep for human/domain review.",
    )


def recommendation_for_family(family: str, handling: str) -> str:
    if family == "article_preposition_title_faith_culture":
        return "Design a parser that labels getter grammatical role and surrounding article/preposition; allow dry-run only after role is known."
    if family == "pantheonterm_agreement":
        return "Design PantheonTerm agreement policy; keep candidate generation disabled until singular/plural behavior is known."
    if family == "clergy_adherent_role_agreement":
        return "Split clergy/adherent/adjective roles; use human review or parser-backed role metadata before any replacement."
    if family == "culture_collective":
        return "Create culture collective parser for article, subject role, and plural agreement; no auto-apply."
    if family == "possessive_or_relation":
        return "Keep in architecture hold; needs perspective-aware relation/possessive policy."
    if family == "getter_perspective_omitted":
        return "Architecture should decide whether omission is allowed, transformable, or a hard preservation error."
    if family == "select_localization_select_cstring":
        return "Route to SelectLocalization/Select_CString parser, not medium_dynamic_light text rules."
    if handling == "human_review":
        return "Small human packet only; no resolver."
    return "Keep as parser-policy input; no candidates until false_safe_risk can be proven 0."


def representative_samples(rows: list[dict[str, Any]], per_family: int = 5) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["architecture_family"])].append(row)
    sample: list[dict[str, Any]] = []
    for family in sorted(grouped):
        sample.extend(grouped[family][:per_family])
    return sample


def top_counter(counter: Counter[str], limit: int = 50) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in counter.most_common(limit)]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def write_txt(path: Path, summary: dict[str, Any], sample: list[dict[str, Any]]) -> None:
    lines = [
        "domain_policy_vote_candidate medium_dynamic_light residual architecture packet",
        "",
        f"segment_state_run_id: {summary['segment_state_run_id']}",
        f"ledger_run_id: {summary['ledger_run_id']}",
        f"packet_count: {summary['packet_count']}",
        "",
        "architecture_family_counts:",
    ]
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["architecture_family_counts"])
    lines.extend(["", "handling_counts:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["handling_counts"])
    lines.extend(["", "surface_family_counts:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["surface_family_counts"])
    lines.extend(["", "policy_parser_recommendations:"])
    for item in summary["policy_parser_recommendations"]:
        lines.append(f"- {item['family']}: {item['recommendation']}")
    lines.extend(["", "representative_examples:"])
    for row in sample:
        lines.extend(
            [
                "",
                f"## segment_id {row['segment_id']} | {row['architecture_family']} | {row['recommended_handling']}",
                f"- surface_bucket: {row.get('surface_bucket')}",
                f"- source_key: {row.get('source_key')}",
                f"- relative_path: {row.get('relative_path')}",
                f"- tokens: {', '.join(row.get('dynamic_tokens') or [])}",
                f"- tags: {', '.join(row.get('architecture_tags') or [])}",
                f"- recommendation: {row.get('family_recommendation')}",
                f"- english_text: {row.get('english_text')}",
                f"- spanish_text: {row.get('spanish_text')}",
                f"- current_output_text: {row.get('current_output_text')}",
            ]
        )
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
    preflight_path, excluded = deep.load_preflight_exclusions()
    with deep.connect_readonly() as conn:
        ledger_run_id = deep.latest_ledger_run_id(conn)
        source_rows = deep.fetch_rows(conn, SEGMENT_STATE_RUN_ID, ledger_run_id, excluded)

    enriched = [deep.enrich_row(row) for row in source_rows]
    medium_rows = [row for row in enriched if row.get("risk_bucket") == EXPECTED_RISK_BUCKET]

    rows: list[dict[str, Any]] = []
    for row in medium_rows:
        family, handling, tags, reason = architecture_family(row)
        family_recommendation = recommendation_for_family(family, handling)
        rows.append(
            {
                **row,
                "architecture_family": family,
                "recommended_handling": handling,
                "architecture_tags": tags,
                "family_reason": reason,
                "family_recommendation": family_recommendation,
                "dynamic_tokens": tokens(row),
                "candidate_generation_allowed": False,
                "apply_allowed": False,
                "lifecycle_allowed": False,
                "architecture_packet_source": SOURCE,
            }
        )
    rows.sort(key=lambda row: (str(row["architecture_family"]), str(row.get("surface_bucket") or ""), int(row["segment_id"])))

    family_counts = Counter(str(row["architecture_family"]) for row in rows)
    handling_counts = Counter(str(row["recommended_handling"]) for row in rows)
    surface_family_counts = Counter(f"{row.get('surface_bucket')} | {row.get('architecture_family')}" for row in rows)
    recommendations = [
        {"family": family, "count": count, "recommendation": recommendation_for_family(family, "")}
        for family, count in family_counts.most_common()
    ]
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "read_only_residual_architecture_packet",
        "segment_state_run_id": SEGMENT_STATE_RUN_ID,
        "ledger_run_id": ledger_run_id,
        "lane": "domain_policy_vote_candidate",
        "risk_bucket": EXPECTED_RISK_BUCKET,
        "source_deep_diagnostic_run": "reports/20260629_165952_496264_domain_policy_vote_candidate_deep_diagnostic_summary.json",
        "preflight_summary_path": str(preflight_path) if preflight_path else None,
        "preflight_excluded_segment_count": len(excluded),
        "packet_count": len(rows),
        "architecture_family_counts": top_counter(family_counts),
        "handling_counts": top_counter(handling_counts),
        "surface_family_counts": top_counter(surface_family_counts),
        "representative_sample_count": len(representative_samples(rows)),
        "policy_parser_recommendations": recommendations,
        "clear_human_count": handling_counts.get("human_review", 0),
        "hold_architecture_count": handling_counts.get("hold_architecture", 0),
        "parser_policy_candidate_count": handling_counts.get("parser_policy_candidate", 0),
        "human_or_parser_policy_count": handling_counts.get("human_or_parser_policy", 0),
        "recommended_next_step": "Send this packet to architecture for parser/policy design; do not generate candidates from medium_dynamic_light residuals until parser guards exist.",
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
    write_txt(txt_path, summary, representative_samples(rows))
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
