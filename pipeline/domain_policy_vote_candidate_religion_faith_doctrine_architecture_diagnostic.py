from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import domain_policy_vote_candidate_deep_diagnostic as base
from domain_policy_vote_candidate_post_medium_dynamic_light_diagnostic import EXCLUDED_MEDIUM_DYNAMIC_LIGHT_SEGMENT_IDS


SOURCE = "domain_policy_vote_candidate_religion_faith_doctrine_architecture_diagnostic_v1"
SEGMENT_STATE_RUN_ID = 504
EXPECTED_COUNT = 341
SURFACE_BUCKET = "religion_faith_doctrine"

PANTHEON_RE = re.compile(
    r"PantheonTerm|HighGod|HighGodName|HighGodNamePossessive|HighGodNameAlternate|"
    r"DivineRealm|Divine|GoodGod|EvilGod|HouseOfWorship",
    re.IGNORECASE,
)
CLERGY_RE = re.compile(
    r"clergy|priest|priesthood|court_chaplain|realm_priest|head_of_faith|"
    r"HeadOfFaith|bishop|chaplain|adherent|adherents|faithful|pilgrim",
    re.IGNORECASE,
)
TENET_DOCTRINE_RE = re.compile(
    r"tenet|doctrine|doctrine_parameter|doctrine_|GetDoctrine|religious_doctrine|"
    r"marriage_doctrine|crime_doctrine",
    re.IGNORECASE,
)
FAITH_GETTER_RE = re.compile(
    r"\b(?:faith|FAITH|ROOT\.Faith|ROOT\.Char\.GetFaith|GetFaith|faith_scope|old_faith|"
    r"religion|religious_leader|GetReligionFamily)\b|"
    r"\[(?:faith|religion)\.|(?:faith|religion)\.Get|FAITH\.|ROOT\.Faith\.",
    re.IGNORECASE,
)
ARTICLE_PREP_RE = re.compile(
    r"\b(?:de|do|da|dos|das|em|no|na|nos|nas|para|pela|pelo|ao|à|a|o|os|as)\s+"
    r"(?:\[?(?:faith|religion|doctrine|tenet|head_of_faith|clergy)|fé|religião|doutrina|clero)",
    re.IGNORECASE,
)
SPANISH_RESIDUE_RE = base.SPANISH_RESIDUE_RE


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def text_blob(row: dict[str, Any]) -> str:
    return " ".join(
        str(row.get(key) or "")
        for key in ("relative_path", "source_key", "english_text", "spanish_text", "current_output_text")
    )


def family_tags(row: dict[str, Any]) -> list[str]:
    text = text_blob(row)
    current = str(row.get("current_output_text") or "")
    tags: list[str] = []
    if PANTHEON_RE.search(text):
        tags.append("PantheonTerm/divine realm/high god names")
    if CLERGY_RE.search(text):
        tags.append("clergy/adherent/priest terms")
    if TENET_DOCTRINE_RE.search(text):
        tags.append("tenet/doctrine dynamic tokens")
    if FAITH_GETTER_RE.search(text):
        tags.append("faith/doctrine getters")
    if ARTICLE_PREP_RE.search(current):
        tags.append("article/preposition/contraction around faith terms")
    if row["risk_bucket"] == "high_multiline_effect_list":
        tags.append("multiline/effect-list structures")
    if row["risk_bucket"] == "high_structural_token_density":
        tags.append("dense structural token clusters")
    if row["risk_bucket"] == "high_spanish_residue_context" or SPANISH_RESIDUE_RE.search(current):
        tags.append("spanish residue context")
    if row["risk_bucket"] == "low_plain_domain":
        tags.append("low/plain leftovers")
    if not tags:
        tags.append("human-only/hold")
    return tags


def primary_family(row: dict[str, Any], tags: list[str]) -> str:
    priority = [
        "PantheonTerm/divine realm/high god names",
        "clergy/adherent/priest terms",
        "tenet/doctrine dynamic tokens",
        "faith/doctrine getters",
        "article/preposition/contraction around faith terms",
        "spanish residue context",
        "multiline/effect-list structures",
        "dense structural token clusters",
        "low/plain leftovers",
        "human-only/hold",
    ]
    for item in priority:
        if item in tags:
            return item
    return "human-only/hold"


def recommendation_for_family(family: str, count: int, high_risk_count: int) -> dict[str, Any]:
    if family == "PantheonTerm/divine realm/high god names":
        return {
            "recommended_action": "hold_until_runtime_agreement_policy",
            "rationale": "Requires number/gender semantics for faith runtime names before any candidate generation.",
        }
    if family == "clergy/adherent/priest terms":
        return {
            "recommended_action": "read_only_subpolicy_review",
            "rationale": "Enough recurring clergy/adherent language to review a splitter, but not enough to apply automatically.",
        }
    if family == "tenet/doctrine dynamic tokens":
        return {
            "recommended_action": "read_only_parser_policy_review",
            "rationale": "Doctrine/tenet tokens are domain-specific and structurally risky; start with splitter/parser review.",
        }
    if family == "faith/doctrine getters":
        return {
            "recommended_action": "read_only_getter_role_policy_review",
            "rationale": "Faith/religion getters need role classification before any text decision.",
        }
    if family == "article/preposition/contraction around faith terms":
        return {
            "recommended_action": "read_only_article_preposition_review",
            "rationale": "Potentially useful, but needs contraction/article guards around dynamic faith terms.",
        }
    if family in {"multiline/effect-list structures", "dense structural token clusters"} or high_risk_count:
        return {
            "recommended_action": "hold_for_architecture",
            "rationale": "High structural risk; do not create human packet or candidate until parser policy exists.",
        }
    if family == "low/plain leftovers":
        return {
            "recommended_action": "small_human_packet_possible",
            "rationale": "Plain leftovers are low volume; only worth a tiny packet if we want to clean edge cases.",
        }
    return {
        "recommended_action": "hold_collect_more_signal",
        "rationale": "Insufficient safe recurring pattern.",
    }


def enrich_architecture_row(row: dict[str, Any]) -> dict[str, Any]:
    tags = family_tags(row)
    family = primary_family(row, tags)
    return {
        **row,
        "architecture_family": family,
        "architecture_family_tags": tags,
        "candidate_generation_allowed": False,
        "auto_apply_allowed": False,
        "lifecycle_allowed": False,
        "production_release_allowed": False,
    }


def top_counter(counter: Counter[str], limit: int = 30) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in counter.most_common(limit)]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def representative_examples(rows: list[dict[str, Any]], per_family: int = 5) -> dict[str, list[dict[str, Any]]]:
    by_family: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_family[row["architecture_family"]].append(row)
    examples: dict[str, list[dict[str, Any]]] = {}
    for family, family_rows in by_family.items():
        ordered = sorted(
            family_rows,
            key=lambda item: (
                item["risk_bucket"] not in {"high_structural_token_density", "high_multiline_effect_list"},
                -int(item.get("token_count") or 0),
                int(item["segment_id"]),
            ),
        )
        examples[family] = [
            {
                "segment_id": row["segment_id"],
                "risk_bucket": row["risk_bucket"],
                "relative_path": row["relative_path"],
                "source_key": row["source_key"],
                "token_count": row["token_count"],
                "current_output_text": row["current_output_text"],
                "english_text": row["english_text"],
            }
            for row in ordered[:per_family]
        ]
    return examples


def write_txt(path: Path, summary: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    lines = [
        "domain_policy_vote_candidate religion_faith_doctrine architecture diagnostic",
        "",
        f"segment_state_run_id: {summary['segment_state_run_id']}",
        f"diagnostic_count: {summary['diagnostic_count']}",
        f"expected_count: {summary['expected_count']}",
        "",
        "architecture_family_counts:",
    ]
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["architecture_family_counts"])
    lines.extend(["", "risk_bucket_counts:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["risk_bucket_counts"])
    lines.extend(["", "family_recommendations:"])
    for rec in summary["family_recommendations"]:
        lines.append(f"- {rec['count']} | {rec['family']} | high_risk={rec['high_risk_count']} | {rec['recommended_action']}")
    lines.extend(["", "representative_examples:"])
    for family, examples in summary["representative_examples"].items():
        lines.extend(["", f"## {family}"])
        for row in examples:
            lines.extend(
                [
                    f"- segment_id {row['segment_id']} | {row['risk_bucket']} | {row['source_key']}",
                    f"  output: {row['current_output_text']}",
                ]
            )
    lines.extend(
        [
            "",
            "operational_recommendation:",
            f"- {summary['single_operational_recommendation']}",
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
    preflight_path, preflight_excluded = base.load_preflight_exclusions()
    excluded = set(preflight_excluded) | EXCLUDED_MEDIUM_DYNAMIC_LIGHT_SEGMENT_IDS
    with base.connect_readonly() as conn:
        ledger_run_id = base.latest_ledger_run_id(conn)
        rows = base.fetch_rows(conn, SEGMENT_STATE_RUN_ID, ledger_run_id, excluded)

    enriched = [base.enrich_row(row) for row in rows]
    religion_rows = [row for row in enriched if row["surface_bucket"] == SURFACE_BUCKET]
    architecture_rows = [enrich_architecture_row(row) for row in religion_rows]
    architecture_rows.sort(key=lambda row: (row["architecture_family"], row["risk_bucket"], int(row["segment_id"])))

    family_counts = Counter(row["architecture_family"] for row in architecture_rows)
    tag_counts = Counter(tag for row in architecture_rows for tag in row["architecture_family_tags"])
    risk_counts = Counter(row["risk_bucket"] for row in architecture_rows)
    family_risk_counts = Counter(f"{row['architecture_family']} | {row['risk_bucket']}" for row in architecture_rows)
    recommendations = []
    for family, count in family_counts.most_common():
        family_rows = [row for row in architecture_rows if row["architecture_family"] == family]
        high_risk_count = sum(
            1
            for row in family_rows
            if row["risk_bucket"] in {"high_structural_token_density", "high_multiline_effect_list", "high_spanish_residue_context"}
        )
        recommendations.append(
            {
                "family": family,
                "count": count,
                "high_risk_count": high_risk_count,
                **recommendation_for_family(family, count, high_risk_count),
            }
        )

    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "read_only_religion_faith_doctrine_architecture_diagnostic",
        "segment_state_run_id": SEGMENT_STATE_RUN_ID,
        "ledger_run_id": ledger_run_id,
        "surface_bucket": SURFACE_BUCKET,
        "diagnostic_count": len(architecture_rows),
        "expected_count": EXPECTED_COUNT,
        "count_matches_expected": len(architecture_rows) == EXPECTED_COUNT,
        "preflight_summary_path": str(preflight_path) if preflight_path else None,
        "preflight_excluded_segment_count": len(preflight_excluded),
        "excluded_medium_dynamic_light_count": len(EXCLUDED_MEDIUM_DYNAMIC_LIGHT_SEGMENT_IDS),
        "architecture_family_counts": top_counter(family_counts),
        "architecture_family_tag_counts": top_counter(tag_counts),
        "risk_bucket_counts": top_counter(risk_counts),
        "family_risk_counts": top_counter(family_risk_counts, 60),
        "family_recommendations": recommendations,
        "representative_examples": representative_examples(architecture_rows),
        "candidate_generation_count": 0,
        "apply_output_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "single_operational_recommendation": (
            "Do not generate candidates. Start with a read-only faith/doctrine getter-role policy review, "
            "while keeping PantheonTerm/runtime divine names and dense/multiline structures in explicit hold."
        ),
        "output_files": {},
    }

    base_path = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_religion_faith_doctrine_architecture_diagnostic"
    txt_path = base_path.with_suffix(".txt")
    jsonl_path = base_path.with_suffix(".jsonl")
    summary_path = Path(str(base_path) + "_summary.json")
    write_jsonl(jsonl_path, architecture_rows)
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary_json": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_txt(txt_path, summary, architecture_rows)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
