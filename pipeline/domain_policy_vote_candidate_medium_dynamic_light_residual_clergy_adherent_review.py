from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_medium_dynamic_light_residual_clergy_adherent_review"
INPUT_PATH = Path("reports/20260629_203140_344770_medium_dynamic_light_residual_parser_policy_dry_run.jsonl")
TARGET_ROUTE = "route_residual_clergy_adherent_role_agreement"
EXPECTED_COUNT = 7


CLASSIFICATION_BY_SEGMENT: dict[int, tuple[str, str, list[str], str]] = {
    34205: (
        "clergy_role_name",
        "hold_or_subpolicy",
        ["court_chaplain_position_token", "clergy_standing_phrase"],
        "Needs clergy-role agreement/politeness context; do not absorb into plain getter or article policy.",
    ),
    126778: (
        "adherent_plural",
        "getter_role_policy",
        ["adherent_plural_token", "community_of_adherents"],
        "Can be routed by getter-role policy as plural adherent role, but still split-only until number/gender metadata exists.",
    ),
    127339: (
        "faith_adjective",
        "getter_role_policy",
        ["faith_adjective_token", "faith_name_after_article"],
        "Can be routed by getter-role policy as faith adjective/name context; no replacement without article/gender parser.",
    ),
    240056: (
        "clergy_role_name",
        "hold_or_subpolicy",
        ["priests_plain_text", "possessive_high_god_token"],
        "Clergy noun is plain text while dynamic token is possessive deity; keep for clergy/adherent subpolicy review.",
    ),
    282943: (
        "clergy_role_name",
        "hold_or_subpolicy",
        ["priests_and_nuns_plain_text", "building_name_token"],
        "Clergy role names are plain text with building token nearby; not suitable for getter/article absorption.",
    ),
    30175: (
        "faith_adjective",
        "getter_role_policy",
        ["faith_adjective_token", "theory_modifier_context", "faithful_plain_text"],
        "Can be routed by getter-role policy as faith adjective, but remains split-only due adjective agreement.",
    ),
    39108: (
        "clergy_role_name",
        "hold_or_subpolicy",
        ["clerics_plain_text", "building_description_variable"],
        "Clergy role is plain text in a building description; better as dedicated clergy/adherent subpolicy or human packet.",
    ),
}


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


def top_counter(counter: Counter[str]) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in counter.most_common()]


def representative_samples(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["subtype_review"])].append(row)
    sample: list[dict[str, Any]] = []
    for subtype in sorted(grouped):
        sample.extend(grouped[subtype][:3])
    return sample


def review_row(row: dict[str, Any]) -> dict[str, Any]:
    segment_id = int(row["segment_id"])
    if segment_id not in CLASSIFICATION_BY_SEGMENT:
        raise SystemExit(f"missing classification for segment_id {segment_id}")
    subtype, absorption, tags, recommendation = CLASSIFICATION_BY_SEGMENT[segment_id]
    return {
        **row,
        "review_source": SOURCE,
        "subtype_review": subtype,
        "role_tags": tags,
        "absorption_recommendation": absorption,
        "role_recommendation": recommendation,
        "candidate_generation_allowed": False,
        "auto_apply_allowed": False,
        "lifecycle_allowed": False,
        "production_release_allowed": False,
    }


def write_txt(path: Path, summary: dict[str, Any], sample: list[dict[str, Any]]) -> None:
    lines = [
        "domain_policy_vote_candidate medium_dynamic_light residual clergy/adherent role review",
        "",
        f"input_path: {summary['input_path']}",
        f"target_route: {summary['target_route']}",
        f"review_count: {summary['review_count']}",
        f"expected_count: {summary['expected_count']}",
        f"count_matches_expected: {str(summary['count_matches_expected']).lower()}",
        "",
        "subtype_counts:",
    ]
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["subtype_counts"])
    lines.extend(["", "absorption_recommendation_counts:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["absorption_recommendation_counts"])
    lines.extend(["", "surface_subtype_counts:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["surface_subtype_counts"])
    lines.extend(["", "representative_examples:"])
    for row in sample:
        lines.extend(
            [
                "",
                f"## segment_id {row['segment_id']} | {row['subtype_review']} | {row['absorption_recommendation']}",
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
    ids = [int(row["segment_id"]) for row in input_rows]
    if len(ids) != len(set(ids)):
        raise SystemExit("duplicate segment_id guard failed")

    rows = [review_row(row) for row in input_rows]
    rows.sort(key=lambda row: (str(row["absorption_recommendation"]), str(row["subtype_review"]), int(row["segment_id"])))

    subtype_counts = Counter(str(row["subtype_review"]) for row in rows)
    absorption_counts = Counter(str(row["absorption_recommendation"]) for row in rows)
    surface_counts = Counter(f"{row.get('surface_bucket')} | {row.get('subtype_review')}" for row in rows)
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
        "absorption_recommendation_counts": top_counter(absorption_counts),
        "surface_subtype_counts": top_counter(surface_counts),
        "representative_sample_count": len(representative_samples(rows)),
        "recommendation": (
            "Absorb only faith_adjective and adherent_plural into medium_dynamic_light_getter_role_policy as split-only routes. "
            "Do not absorb clergy_role_name into article/preposition policy; keep it in hold or design a narrow clergy/adherent subpolicy after more review."
        ),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
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
