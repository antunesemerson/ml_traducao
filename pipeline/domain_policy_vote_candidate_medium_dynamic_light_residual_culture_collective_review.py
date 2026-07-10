from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_medium_dynamic_light_residual_culture_collective_review"
INPUT_PATH = Path("reports/20260629_203140_344770_medium_dynamic_light_residual_parser_policy_dry_run.jsonl")
TARGET_ROUTE = "route_residual_culture_collective"
EXPECTED_COUNT = 6


CLASSIFICATION_BY_SEGMENT: dict[int, tuple[str, str, list[str], str]] = {
    20545: (
        "requires_plural_agreement",
        "hold_or_subpolicy",
        ["collective_subject_apposition", "article_plural_before_collective", "my_people_context"],
        "Collective noun is appositive to 'meu povo' but drives plural verb agreement; needs a culture-collective parser.",
    ),
    20555: (
        "culture_collective_subject",
        "hold_or_subpolicy",
        ["collective_subject", "article_plural_before_collective", "plural_verb_agreement"],
        "Subject role is clear, but article/number agreement must be modeled before any automatic handling.",
    ),
    20573: (
        "requires_plural_agreement",
        "hold_or_subpolicy",
        ["first_person_collective_apposition", "no_article_collective", "plural_verb_agreement"],
        "First-person apposition needs subject/person agreement; hold for a dedicated culture-collective parser.",
    ),
    162589: (
        "culture_collective_adjective_like",
        "getter_role_policy",
        ["culture_name_as_people_modifier", "adjective_like_culture_token"],
        "Can be absorbed by getter-role policy as culture-name/adjective-like route, split-only.",
    ),
    164837: (
        "culture_collective_object",
        "article_preposition_policy",
        ["preposition_com_article_plural", "collective_object"],
        "Can be absorbed by article/preposition policy as 'com os [culture collective]' context, split-only.",
    ),
    164838: (
        "culture_collective_object",
        "article_preposition_policy",
        ["preposition_com_article_plural", "collective_object", "second_perspective"],
        "Can be absorbed by article/preposition policy as 'com os [culture collective]' context, split-only.",
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
        "domain_policy_vote_candidate medium_dynamic_light residual culture collective review",
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
            "Absorb only the object/preposition cases into medium_dynamic_light_article_preposition_policy and the adjective-like culture-name case into getter_role_policy, all split-only. "
            "Keep collective subject/plural-agreement cases in hold or design a narrow culture-collective parser; they need article, number, and subject/person agreement."
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
