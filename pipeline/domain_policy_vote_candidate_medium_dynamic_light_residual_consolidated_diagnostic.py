from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_medium_dynamic_light_residual_consolidated_diagnostic"

RESIDUAL_SUMMARY = Path("reports/20260629_203140_344770_medium_dynamic_light_residual_parser_policy_dry_run_summary.json")
GENERIC_ABSORPTION_SUMMARY = Path("reports/20260629_214916_476891_medium_dynamic_light_generic_getter_absorption_dry_run_v1_summary.json")
CLERGY_SUMMARY = Path("reports/20260629_220306_916060_domain_policy_vote_candidate_medium_dynamic_light_residual_clergy_adherent_review_summary.json")
CULTURE_SUMMARY = Path("reports/20260629_222740_237946_domain_policy_vote_candidate_medium_dynamic_light_residual_culture_collective_review_summary.json")
ARTICLE_SUMMARY = Path("reports/20260629_210541_103707_medium_dynamic_light_article_preposition_policy_dry_run_summary.json")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def counter_from_items(items: list[dict[str, Any]]) -> Counter[str]:
    return Counter({str(item["key"]): int(item["count"]) for item in items})


def top_counter(counter: Counter[str]) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in counter.most_common()]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def write_txt(path: Path, summary: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    lines = [
        "medium_dynamic_light residual consolidated diagnostic",
        "",
        f"review_count: {summary['review_count']}",
        f"coverage_total: {summary['coverage_total']}",
        f"coverage_matches_residual: {str(summary['coverage_matches_residual']).lower()}",
        f"registry_guard_ok: {str(summary['registry_guard_ok']).lower()}",
        "",
        "final_operational_counts:",
    ]
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["final_operational_counts"])
    lines.extend(["", "absorbed_by_policy_counts:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["absorbed_by_policy_counts"])
    lines.extend(["", "inactive_or_delegated_routes:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["inactive_or_delegated_routes"])
    lines.extend(["", "remaining_residual_families:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["remaining_residual_families"])
    lines.extend(["", "consolidated_rows:"])
    for row in rows:
        lines.extend(
            [
                "",
                f"## {row['source_family']} -> {row['final_bucket']} ({row['count']})",
                f"- target_policy: {row['target_policy']}",
                f"- operational_status: {row['operational_status']}",
                f"- route_active_after_consolidation: {str(row['route_active_after_consolidation']).lower()}",
                f"- note: {row['note']}",
            ]
        )
    lines.extend(
        [
            "",
            "registry_note_recommendations:",
        ]
    )
    lines.extend(f"- {item}" for item in summary["registry_note_recommendations"])
    lines.extend(
        [
            "",
            "recommendation:",
            summary["single_operational_recommendation"],
            "",
            "gates:",
            "- candidate_generation: not_run",
            "- apply_output: not_run",
            "- lifecycle: not_run",
            "- segment_state: not_run",
            "- reindex: not_run",
            "- full_production: not_run",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    residual = read_json(RESIDUAL_SUMMARY)
    generic = read_json(GENERIC_ABSORPTION_SUMMARY)
    clergy = read_json(CLERGY_SUMMARY)
    culture = read_json(CULTURE_SUMMARY)
    article = read_json(ARTICLE_SUMMARY)

    if int(residual["review_count"]) != 81:
        raise SystemExit("residual review_count guard failed")
    if int(generic["review_count"]) != 26 or generic.get("generic_getter_role_unknown_active_after_absorption") is not False:
        raise SystemExit("generic absorption guard failed")
    if int(clergy["review_count"]) != 7:
        raise SystemExit("clergy review_count guard failed")
    if int(culture["review_count"]) != 6:
        raise SystemExit("culture review_count guard failed")
    if int(article["review_count"]) != 21:
        raise SystemExit("article review_count guard failed")

    article_status = counter_from_items(article["route_status_counts"])
    generic_policy = counter_from_items(generic["absorbed_policy_counts"])
    clergy_absorption = counter_from_items(clergy["absorption_recommendation_counts"])
    culture_absorption = counter_from_items(culture["absorption_recommendation_counts"])
    residual_families = counter_from_items(residual["architecture_family_counts"])

    article_policy_count = (
        article_status["split_only_no_candidate"]
        + generic_policy["medium_dynamic_light_article_preposition_policy"]
        + culture_absorption["article_preposition_policy"]
    )
    getter_policy_count = (
        generic_policy["medium_dynamic_light_getter_role_policy"]
        + clergy_absorption["getter_role_policy"]
        + culture_absorption["getter_role_policy"]
    )
    holds = Counter(
        {
            "residual_medium_dynamic_light_human": residual_families["residual_medium_dynamic_light_human"],
            "article_preposition_uncertain": article_status["hold_context"],
            "generic_relation_or_possessive": generic_policy["hold_context"],
            "clergy_role_name_hold_or_subpolicy": clergy_absorption["hold_or_subpolicy"],
            "culture_collective_hold_or_subpolicy": culture_absorption["hold_or_subpolicy"],
            "getter_perspective_omitted": residual_families["getter_perspective_omitted"],
            "pantheonterm_agreement": residual_families["pantheonterm_agreement"],
            "select_localization_select_cstring": residual_families["select_localization_select_cstring"],
        }
    )
    hold_total = sum(holds.values())
    coverage_total = article_policy_count + getter_policy_count + hold_total

    rows = [
        {
            "source_family": "article_preposition_title_faith_culture",
            "count": article_status["split_only_no_candidate"],
            "final_bucket": "absorbed_existing_policy",
            "target_policy": "medium_dynamic_light_article_preposition_policy",
            "operational_status": "split_only_no_candidate",
            "route_active_after_consolidation": False,
            "note": "Original residual article/preposition family is now delegated to article/preposition policy.",
        },
        {
            "source_family": "generic_getter_role_unknown",
            "count": generic_policy["medium_dynamic_light_article_preposition_policy"],
            "final_bucket": "absorbed_existing_policy",
            "target_policy": "medium_dynamic_light_article_preposition_policy",
            "operational_status": "split_only_no_candidate",
            "route_active_after_consolidation": False,
            "note": "Article/preposition-context unknown getters are absorbed by existing article/preposition policy.",
        },
        {
            "source_family": "generic_getter_role_unknown",
            "count": generic_policy["medium_dynamic_light_getter_role_policy"],
            "final_bucket": "absorbed_existing_policy",
            "target_policy": "medium_dynamic_light_getter_role_policy",
            "operational_status": "split_only_no_candidate",
            "route_active_after_consolidation": False,
            "note": "Getter-role unknowns are absorbed by existing getter-role policy.",
        },
        {
            "source_family": "clergy_adherent_role_agreement",
            "count": clergy_absorption["getter_role_policy"],
            "final_bucket": "absorbed_existing_policy",
            "target_policy": "medium_dynamic_light_getter_role_policy",
            "operational_status": "split_only_no_candidate",
            "route_active_after_consolidation": True,
            "note": "Only faith_adjective/adherent_plural items are safe as split-only getter-role routes.",
        },
        {
            "source_family": "culture_collective",
            "count": culture_absorption["article_preposition_policy"],
            "final_bucket": "absorbed_existing_policy",
            "target_policy": "medium_dynamic_light_article_preposition_policy",
            "operational_status": "split_only_no_candidate",
            "route_active_after_consolidation": True,
            "note": "Only object/preposition collective-culture items are delegated to article/preposition policy.",
        },
        {
            "source_family": "culture_collective",
            "count": culture_absorption["getter_role_policy"],
            "final_bucket": "absorbed_existing_policy",
            "target_policy": "medium_dynamic_light_getter_role_policy",
            "operational_status": "split_only_no_candidate",
            "route_active_after_consolidation": True,
            "note": "Adjective-like culture-name item is delegated to getter-role policy.",
        },
    ]
    for family, count in holds.items():
        rows.append(
            {
                "source_family": family,
                "count": count,
                "final_bucket": "hold",
                "target_policy": "hold_context",
                "operational_status": "hold_context",
                "route_active_after_consolidation": family in {
                    "clergy_role_name_hold_or_subpolicy",
                    "culture_collective_hold_or_subpolicy",
                    "getter_perspective_omitted",
                    "pantheonterm_agreement",
                    "select_localization_select_cstring",
                },
                "note": "Remain held for human review, architecture, or a later narrow parser; no candidate generation.",
            }
        )

    final_counts = Counter(
        {
            "medium_dynamic_light_article_preposition_policy": article_policy_count,
            "medium_dynamic_light_getter_role_policy": getter_policy_count,
            "hold_context": hold_total,
        }
    )
    inactive_routes = Counter(
        {
            "route_residual_generic_getter_role_unknown": residual_families["generic_getter_role_unknown"],
            "route_residual_article_preposition_title_faith_culture": residual_families["article_preposition_title_faith_culture"],
        }
    )
    remaining_residual = Counter(
        {
            "residual_medium_dynamic_light_human": residual_families["residual_medium_dynamic_light_human"],
            "getter_perspective_omitted": residual_families["getter_perspective_omitted"],
            "clergy_role_name_hold_or_subpolicy": clergy_absorption["hold_or_subpolicy"],
            "culture_collective_hold_or_subpolicy": culture_absorption["hold_or_subpolicy"],
            "pantheonterm_agreement": residual_families["pantheonterm_agreement"],
            "select_localization_select_cstring": residual_families["select_localization_select_cstring"],
            "generic_relation_or_possessive": generic_policy["hold_context"],
            "article_preposition_uncertain": article_status["hold_context"],
        }
    )

    registry_guard_ok = bool(
        residual.get("registry_guard_ok")
        and generic.get("registry_guard_ok")
        and article.get("registry_guard_ok")
    )
    needs_registry_notes_update = True
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "read_only_consolidated_diagnostic",
        "input_paths": {
            "residual_parser_dry_run": str(RESIDUAL_SUMMARY),
            "generic_getter_absorption": str(GENERIC_ABSORPTION_SUMMARY),
            "clergy_adherent_review": str(CLERGY_SUMMARY),
            "culture_collective_review": str(CULTURE_SUMMARY),
            "article_preposition_policy_dry_run": str(ARTICLE_SUMMARY),
        },
        "review_count": int(residual["review_count"]),
        "coverage_total": coverage_total,
        "coverage_matches_residual": coverage_total == int(residual["review_count"]),
        "absorbed_by_policy_counts": top_counter(
            Counter(
                {
                    "medium_dynamic_light_article_preposition_policy": article_policy_count,
                    "medium_dynamic_light_getter_role_policy": getter_policy_count,
                }
            )
        ),
        "final_operational_counts": top_counter(final_counts),
        "hold_breakdown": top_counter(holds),
        "inactive_or_delegated_routes": top_counter(inactive_routes),
        "remaining_residual_families": top_counter(remaining_residual),
        "needs_registry_notes_update": needs_registry_notes_update,
        "registry_guard_ok": registry_guard_ok,
        "registry_note_recommendations": [
            "Update medium_dynamic_light_article_preposition_policy notes to include absorbed generic_getter article/preposition contexts (+14) and culture_collective object/preposition contexts (+2).",
            "Update medium_dynamic_light_getter_role_policy notes to include absorbed generic getter roles (+11), clergy/adherent faith/adherent routes (+3), and culture adjective-like route (+1).",
            "Mark route_residual_generic_getter_role_unknown as operationally delegated/inactive after absorption.",
            "Mark route_residual_article_preposition_title_faith_culture as operationally delegated to medium_dynamic_light_article_preposition_policy.",
            "Keep candidate_generation_allowed=false, auto_apply_allowed=false, lifecycle_allowed=false, production_release_allowed=false.",
        ],
        "new_subpolicy_volume_assessment": [
            {
                "family": "residual_medium_dynamic_light_human",
                "count": 12,
                "recommendation": "human_packet_or_hold; not parser subpolicy yet",
            },
            {
                "family": "getter_perspective_omitted",
                "count": 5,
                "recommendation": "architecture/parser later; volume low",
            },
            {
                "family": "clergy_role_name_hold_or_subpolicy",
                "count": 4,
                "recommendation": "hold; collect more examples before subpolicy",
            },
            {
                "family": "culture_collective_hold_or_subpolicy",
                "count": 3,
                "recommendation": "hold; collect more examples before subpolicy",
            },
            {
                "family": "pantheonterm_agreement",
                "count": 2,
                "recommendation": "hold; too small for new subpolicy",
            },
            {
                "family": "select_localization_select_cstring",
                "count": 2,
                "recommendation": "architecture-only if recurring; too small now",
            },
        ],
        "single_operational_recommendation": (
            "Do not create a new residual policy now. Update registry/notes for the two existing splitters to document the 51 absorbed split-only routes, "
            "mark generic_getter_role_unknown and article_preposition_title_faith_culture as delegated/inactive, and keep the remaining 30 items in hold or small human/architecture review."
        ),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "gates": {
            "candidate_generation": "not_run",
            "apply_output": "not_run",
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
    if not summary["coverage_matches_residual"]:
        raise SystemExit(f"coverage guard failed: {coverage_total} != {residual['review_count']}")

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
