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


SOURCE = "domain_policy_vote_candidate_religion_faith_doctrine_post507_hold_diagnostic_v1"
SEGMENT_STATE_RUN_ID = 507
SURFACE_BUCKET = "religion_faith_doctrine"

DEBUG_RE = re.compile(r"#D\b|PLACEHOLDER|\bTODO\b|\bDEBUG\b|LOC_PLACEHOLDER|placeholder", re.IGNORECASE)
HOLY_SITE_RE = re.compile(r"holy_site|holy site|holy_sites|sacred site|County Conversion|Conversion Speed|Conversion Resistance", re.IGNORECASE)
HOLY_WAR_RE = re.compile(r"GHWName|great[_ ]holy|holy[_ ]war|crusade|jihad|fervor|cruzad|guerra santa", re.IGNORECASE)
RELIGION_FAMILY_RE = re.compile(r"GetReligionByKey|GetReligionFamily|ReligionFamily|religious_family|rf_", re.IGNORECASE)
FAITH_ADJECTIVE_RE = re.compile(r"Faith\.GetAdjective|GetFaith\.GetAdjective|FAITH\.GetAdjective|faith.*GetAdjective", re.IGNORECASE)
FAITH_NAME_RE = re.compile(r"Faith\.GetName|GetFaith\.GetName|FAITH\.GetName|faith\.GetName|GetFaithName", re.IGNORECASE)
FAITH_POSSESSIVE_RE = re.compile(r"Possessive|GetNamePossessive|GetHerHis|GetHisHer|GetTheir", re.IGNORECASE)
DIVINE_RE = re.compile(r"PantheonTerm|HighGod|HighGodName|GoodGod|EvilGod|DivineRealm|HouseOfWorship|ReligiousText|DevilName", re.IGNORECASE)
CLERGY_RE = re.compile(r"Priest|Clergy|HeadOfFaith|head_of_faith|ReligiousHead|Bishop|Chaplain|Adherent|faithful|clergy|priest", re.IGNORECASE)
TENET_DOCTRINE_RE = re.compile(r"tenet|doctrine|GetDoctrine|doctrine_parameter|religious_doctrine|dogma", re.IGNORECASE)
ARTICLE_PREP_RE = re.compile(r"\b(?:de|do|da|dos|das|em|no|na|nos|nas|para|pela|pelo|ao|a|o|os|as)\s+\[?(?:faith|religion|doctrine|tenet)", re.IGNORECASE)


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def text_blob(row: dict[str, Any]) -> str:
    return "\n".join(
        str(row.get(key) or "")
        for key in ("relative_path", "source_key", "english_text", "spanish_text", "current_output_text")
    )


def hold_family(row: dict[str, Any]) -> tuple[str, list[str], str]:
    text = text_blob(row)
    current = str(row.get("current_output_text") or "")
    tags: list[str] = []
    if row.get("risk_bucket") == "high_structural_token_density":
        tags.append("high_structural_token_density")
    if row.get("risk_bucket") == "high_multiline_effect_list":
        tags.append("high_multiline_effect_list")
    if row.get("risk_bucket") == "high_spanish_residue_context":
        tags.append("high_spanish_residue_context")
    if ARTICLE_PREP_RE.search(current):
        tags.append("article_preposition_surface")
    if DEBUG_RE.search(text):
        tags.append("debug_or_placeholder_surface")
        return "debug_placeholder_hold", tags, "hold_explicit_debug_placeholder"
    if HOLY_SITE_RE.search(text):
        tags.append("holy_site_surface")
        return "holy_site_effect_or_requirement", tags, "architecture_or_subpolicy_later"
    if FAITH_ADJECTIVE_RE.search(text) and HOLY_WAR_RE.search(text):
        tags.extend(["faith_adjective_surface", "holy_war_surface"])
        return "faith_adjective_holy_war_runtime", tags, "architecture_parser_later"
    if HOLY_WAR_RE.search(text):
        tags.append("holy_war_or_fervor_surface")
        return "holy_war_fervor_runtime", tags, "architecture_or_subpolicy_later"
    if RELIGION_FAMILY_RE.search(text):
        tags.append("religion_family_runtime_surface")
        return "religion_family_runtime_adjective", tags, "architecture_parser_later"
    if FAITH_POSSESSIVE_RE.search(text):
        tags.append("faith_possessive_surface")
        return "faith_possessive_or_relation", tags, "hold_context_runtime_semantics"
    if DIVINE_RE.search(text):
        tags.append("divine_runtime_surface")
        return "pantheon_divine_runtime_name", tags, "hold_until_runtime_agreement_policy"
    if CLERGY_RE.search(text):
        tags.append("clergy_adherent_surface")
        return "clergy_adherent_priest_terms", tags, "read_only_subpolicy_review"
    if TENET_DOCTRINE_RE.search(text):
        tags.append("tenet_doctrine_surface")
        return "tenet_doctrine_dynamic_tokens", tags, "read_only_parser_policy_review"
    if FAITH_NAME_RE.search(text) and "article_preposition_surface" in tags:
        tags.append("faith_name_surface")
        return "faith_name_article_preposition_context", tags, "architecture_parser_later"
    if FAITH_ADJECTIVE_RE.search(text):
        tags.append("faith_adjective_surface")
        return "faith_adjective_runtime", tags, "hold_context_runtime_semantics"
    if FAITH_NAME_RE.search(text):
        tags.append("faith_name_surface")
        return "faith_name_runtime", tags, "hold_context_runtime_semantics"
    if row.get("risk_bucket") == "high_multiline_effect_list":
        return "multiline_effect_list_hold", tags, "hold_for_architecture"
    if row.get("risk_bucket") == "high_structural_token_density":
        return "dense_structural_token_cluster", tags, "hold_for_architecture"
    if row.get("risk_bucket") == "low_plain_domain":
        return "low_plain_leftover", tags, "small_human_packet_possible"
    return "human_only_or_unknown_hold", tags or ["no_specific_hold_family_signal"], "hold_collect_more_signal"


def top_counter(counter: Counter[str], limit: int = 50) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in counter.most_common(limit)]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def representative_examples(rows: list[dict[str, Any]], per_family: int = 5) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["hold_family"])].append(row)
    examples: dict[str, list[dict[str, Any]]] = {}
    for family, family_rows in grouped.items():
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
                "source_key": row["source_key"],
                "relative_path": row["relative_path"],
                "current_output_text": row["current_output_text"],
                "english_text": row["english_text"],
                "recommended_action": row["recommended_action"],
            }
            for row in ordered[:per_family]
        ]
    return examples


def operational_recommendation(family_counts: Counter[str], high_risk_counts: Counter[str]) -> str:
    if family_counts.get("debug_placeholder_hold", 0) >= 20:
        return (
            "Ask architecture to make debug/placeholder localization an explicit terminal hold/closed-ignore policy "
            "before any new human packet in this lane."
        )
    parser_targets = [
        "religion_family_runtime_adjective",
        "faith_adjective_holy_war_runtime",
        "holy_site_effect_or_requirement",
        "holy_war_fervor_runtime",
    ]
    best = max(parser_targets, key=lambda family: family_counts.get(family, 0))
    if family_counts.get(best, 0) >= 5:
        return f"Prepare architecture parser review for {best}; do not generate candidates from this residual set."
    return "Hold religion_faith_doctrine residuals and move to another macro group; no safe automation pocket is visible."


def write_txt(path: Path, summary: dict[str, Any]) -> None:
    lines = [
        "domain_policy_vote_candidate religion_faith_doctrine post507 hold diagnostic",
        "",
        f"segment_state_run_id: {summary['segment_state_run_id']}",
        f"diagnostic_count: {summary['diagnostic_count']}",
        f"needs_output_apply_count: {summary['needs_output_apply_count']}",
        "",
        "hold_family_counts:",
    ]
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["hold_family_counts"])
    lines.extend(["", "risk_bucket_counts:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["risk_bucket_counts"])
    lines.extend(["", "family_risk_counts:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["family_risk_counts"])
    lines.extend(["", "representative_examples:"])
    for family, examples in summary["representative_examples"].items():
        lines.extend(["", f"## {family}"])
        for row in examples:
            lines.extend(
                [
                    f"- segment_id {row['segment_id']} | {row['risk_bucket']} | {row['recommended_action']} | {row['source_key']}",
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
    diagnostic_rows: list[dict[str, Any]] = []
    for row in religion_rows:
        family, tags, action = hold_family(row)
        diagnostic_rows.append(
            {
                **row,
                "hold_family": family,
                "hold_family_tags": tags,
                "recommended_action": action,
                "candidate_generation_allowed": False,
                "auto_apply_allowed": False,
                "lifecycle_allowed": False,
                "production_release_allowed": False,
            }
        )
    diagnostic_rows.sort(key=lambda row: (str(row["hold_family"]), str(row["risk_bucket"]), int(row["segment_id"])))

    family_counts = Counter(str(row["hold_family"]) for row in diagnostic_rows)
    risk_counts = Counter(str(row["risk_bucket"]) for row in diagnostic_rows)
    family_risk_counts = Counter(f"{row['hold_family']} | {row['risk_bucket']}" for row in diagnostic_rows)
    action_counts = Counter(str(row["recommended_action"]) for row in diagnostic_rows)
    summary: dict[str, Any] = {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "read_only_post507_hold_diagnostic",
        "segment_state_run_id": SEGMENT_STATE_RUN_ID,
        "ledger_run_id": ledger_run_id,
        "preflight_summary_path": str(preflight_path) if preflight_path else None,
        "preflight_excluded_segment_count": len(preflight_excluded),
        "excluded_medium_dynamic_light_count": len(EXCLUDED_MEDIUM_DYNAMIC_LIGHT_SEGMENT_IDS),
        "surface_bucket": SURFACE_BUCKET,
        "diagnostic_count": len(diagnostic_rows),
        "needs_output_apply_count": sum(1 for row in diagnostic_rows if int(row.get("needs_output_apply") or 0) != 0),
        "confirmed_matches_output_count": sum(1 for row in diagnostic_rows if int(row.get("confirmed_matches_output") or 0) == 1),
        "hold_family_counts": top_counter(family_counts),
        "risk_bucket_counts": top_counter(risk_counts),
        "family_risk_counts": top_counter(family_risk_counts),
        "recommended_action_counts": top_counter(action_counts),
        "representative_examples": representative_examples(diagnostic_rows),
        "single_operational_recommendation": operational_recommendation(family_counts, family_risk_counts),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "output_files": {},
    }

    base_path = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_religion_faith_doctrine_post507_hold_diagnostic"
    txt_path = base_path.with_suffix(".txt")
    jsonl_path = base_path.with_suffix(".jsonl")
    summary_path = Path(str(base_path) + "_summary.json")
    write_jsonl(jsonl_path, diagnostic_rows)
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_txt(txt_path, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
