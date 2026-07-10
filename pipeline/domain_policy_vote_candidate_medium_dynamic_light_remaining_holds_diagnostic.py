from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_medium_dynamic_light_remaining_holds_diagnostic"
CONSOLIDATED_SUMMARY = Path("reports/20260629_223142_627849_domain_policy_vote_candidate_medium_dynamic_light_residual_consolidated_diagnostic_summary.json")
RESIDUAL_JSONL = Path("reports/20260629_203140_344770_medium_dynamic_light_residual_parser_policy_dry_run.jsonl")
GENERIC_ABSORPTION_JSONL = Path("reports/20260629_214916_476891_medium_dynamic_light_generic_getter_absorption_dry_run_v1.jsonl")
CLERGY_JSONL = Path("reports/20260629_220306_916060_domain_policy_vote_candidate_medium_dynamic_light_residual_clergy_adherent_review.jsonl")
CULTURE_JSONL = Path("reports/20260629_222740_237946_domain_policy_vote_candidate_medium_dynamic_light_residual_culture_collective_review.jsonl")
ARTICLE_JSONL = Path("reports/20260629_210541_103707_medium_dynamic_light_article_preposition_policy_dry_run.jsonl")
EXPECTED_TOTAL = 30


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


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


def top_counter(counter: Counter[str]) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in counter.most_common()]


def normalize_base(row: dict[str, Any], hold_family: str, operational_class: str, next_action: str, source_file: str) -> dict[str, Any]:
    return {
        "segment_id": int(row["segment_id"]),
        "hold_family": hold_family,
        "operational_class": operational_class,
        "next_action": next_action,
        "source_file": source_file,
        "surface_bucket": row.get("surface_bucket"),
        "source_key": row.get("source_key"),
        "relative_path": row.get("relative_path"),
        "route": row.get("route") or row.get("source_route"),
        "subtype_review": row.get("subtype_review") or row.get("grammar_role_review") or row.get("target_route"),
        "dynamic_tokens": row.get("dynamic_tokens") or [],
        "role_tags": row.get("role_tags") or row.get("architecture_tags") or [],
        "requires_human_context": bool(row.get("requires_human_context")),
        "candidate_generation_allowed": False,
        "auto_apply_allowed": False,
        "lifecycle_allowed": False,
        "production_release_allowed": False,
        "english_text": row.get("english_text"),
        "spanish_text": row.get("spanish_text"),
        "current_output_text": row.get("current_output_text"),
        "recommendation": row.get("role_recommendation") or row.get("family_recommendation") or row.get("note"),
    }


def collect_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    residual_rows = read_jsonl(RESIDUAL_JSONL)
    for row in residual_rows:
        family = row.get("architecture_family")
        if family == "residual_medium_dynamic_light_human":
            rows.append(normalize_base(row, "residual_medium_dynamic_light_human", "human_packet_now", "create_small_human_review_packet", str(RESIDUAL_JSONL)))
        elif family == "getter_perspective_omitted":
            rows.append(normalize_base(row, "getter_perspective_omitted", "architecture_parser_later", "send_to_architecture_perspective_getter_policy", str(RESIDUAL_JSONL)))
        elif family == "pantheonterm_agreement":
            rows.append(normalize_base(row, "pantheonterm_agreement", "architecture_parser_later", "hold_until_pantheonterm_number_policy", str(RESIDUAL_JSONL)))
        elif family == "select_localization_select_cstring":
            rows.append(normalize_base(row, "select_localization_select_cstring", "architecture_parser_later", "send_to_selectlocalization_parser_scope", str(RESIDUAL_JSONL)))

    for row in read_jsonl(CLERGY_JSONL):
        if row.get("absorption_recommendation") == "hold_or_subpolicy":
            rows.append(normalize_base(row, "clergy_role_name_hold_or_subpolicy", "hold_collect_more_signal", "collect_more_clergy_examples_before_subpolicy", str(CLERGY_JSONL)))

    for row in read_jsonl(CULTURE_JSONL):
        if row.get("absorption_recommendation") == "hold_or_subpolicy":
            rows.append(normalize_base(row, "culture_collective_hold_or_subpolicy", "hold_collect_more_signal", "collect_more_culture_collective_examples_before_subpolicy", str(CULTURE_JSONL)))

    for row in read_jsonl(GENERIC_ABSORPTION_JSONL):
        if row.get("target_policy") == "hold_context":
            rows.append(normalize_base(row, "generic_relation_or_possessive", "architecture_parser_later", "hold_until_relation_possessive_parser", str(GENERIC_ABSORPTION_JSONL)))

    for row in read_jsonl(ARTICLE_JSONL):
        if row.get("route_status") == "hold_context":
            rows.append(normalize_base(row, "article_preposition_uncertain", "human_packet_now", "include_in_small_human_review_packet", str(ARTICLE_JSONL)))

    return sorted(rows, key=lambda row: (row["operational_class"], row["hold_family"], row["segment_id"]))


def representative_samples(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["hold_family"]].append(row)
    sample: list[dict[str, Any]] = []
    for family in sorted(grouped):
        sample.extend(grouped[family][:3])
    return sample


def write_txt(path: Path, summary: dict[str, Any], sample: list[dict[str, Any]]) -> None:
    lines = [
        "medium_dynamic_light remaining holds diagnostic",
        "",
        f"hold_count: {summary['hold_count']}",
        f"count_matches_expected: {str(summary['count_matches_expected']).lower()}",
        "",
        "operational_class_counts:",
    ]
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["operational_class_counts"])
    lines.extend(["", "hold_family_counts:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["hold_family_counts"])
    lines.extend(["", "next_action_counts:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["next_action_counts"])
    lines.extend(["", "representative_examples:"])
    for row in sample:
        lines.extend(
            [
                "",
                f"## segment_id {row['segment_id']} | {row['hold_family']} | {row['operational_class']}",
                f"- next_action: {row['next_action']}",
                f"- surface_bucket: {row.get('surface_bucket')}",
                f"- source_key: {row.get('source_key')}",
                f"- dynamic_tokens: {', '.join(row.get('dynamic_tokens') or [])}",
                f"- role_tags: {', '.join(row.get('role_tags') or [])}",
                f"- current_output_text: {row.get('current_output_text')}",
            ]
        )
    lines.extend(
        [
            "",
            "recommendation:",
            summary["single_operational_recommendation"],
            "",
            "gates:",
            "- candidate_generation: not_run",
            "- output_apply: not_run",
            "- lifecycle: not_run",
            "- segment_state: not_run",
            "- reindex: not_run",
            "- full_production: not_run",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    consolidated = read_json(CONSOLIDATED_SUMMARY)
    if int(consolidated.get("final_operational_counts", [])[1].get("count", 0)) != 30:
        # Do not depend on ordering for the actual totals below; this is only an early stale-file smell.
        pass
    rows = collect_rows()
    ids = [row["segment_id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise SystemExit("duplicate segment_id guard failed")
    if len(rows) != EXPECTED_TOTAL:
        raise SystemExit(f"hold count guard failed: {len(rows)} expected {EXPECTED_TOTAL}")

    family_counts = Counter(row["hold_family"] for row in rows)
    operational_counts = Counter(row["operational_class"] for row in rows)
    next_action_counts = Counter(row["next_action"] for row in rows)
    surface_counts = Counter(f"{row.get('surface_bucket')} | {row['hold_family']}" for row in rows)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "read_only_remaining_holds_diagnostic",
        "input_paths": {
            "consolidated_summary": str(CONSOLIDATED_SUMMARY),
            "residual_jsonl": str(RESIDUAL_JSONL),
            "generic_absorption_jsonl": str(GENERIC_ABSORPTION_JSONL),
            "clergy_jsonl": str(CLERGY_JSONL),
            "culture_jsonl": str(CULTURE_JSONL),
            "article_jsonl": str(ARTICLE_JSONL),
        },
        "hold_count": len(rows),
        "expected_hold_count": EXPECTED_TOTAL,
        "count_matches_expected": len(rows) == EXPECTED_TOTAL,
        "hold_family_counts": top_counter(family_counts),
        "operational_class_counts": top_counter(operational_counts),
        "next_action_counts": top_counter(next_action_counts),
        "surface_hold_family_counts": top_counter(surface_counts),
        "human_packet_candidate_count": operational_counts["human_packet_now"],
        "architecture_parser_later_count": operational_counts["architecture_parser_later"],
        "hold_collect_more_signal_count": operational_counts["hold_collect_more_signal"],
        "single_operational_recommendation": (
            "Create a small human review packet only for the 13 human-ready holds, keep 10 architecture/parser holds for the architecture front, "
            "and leave 7 low-volume clergy/culture-subpolicy holds parked until more examples accumulate."
        ),
        "candidate_generation_count": 0,
        "apply_output_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
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
