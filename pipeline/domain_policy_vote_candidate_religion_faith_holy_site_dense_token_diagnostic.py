from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_religion_faith_holy_site_dense_token_diagnostic_v1"
INPUT_JSONL_PATH = Path(
    "reports/20260630_124634_175870_domain_policy_vote_candidate_religion_faith_holy_site_effect_requirement_review.jsonl"
)
EXPECTED_SEGMENT_STATE_RUN_ID = 509
EXPECTED_COUNT = 78

EFFECT_NAME_KEY_RE = re.compile(r"^holy_site_.+_effect_name$")
CONTROLLED_COUNT_RE = re.compile(r"CONTROLLED_HOLY_SITES_TT$")
COUNT_REQUIREMENT_RE = re.compile(r"HOLD_AT_LEAST_COUNT_HOLY_SITES")
CONTROL_WARNING_RE = re.compile(r"HOLY_SITE_NOT_UNDER_CONTROL")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def classify(row: dict[str, Any]) -> tuple[str, str, str]:
    key = str(row.get("source_key") or "")
    if CONTROLLED_COUNT_RE.search(key):
        return (
            "controlled_holy_sites_count_tooltip",
            "parser_later_count_and_possessive",
            "Counter tooltip uses adherent plural + controlled/total holy-site getters; needs parser for possessive/count relation.",
        )
    if CONTROL_WARNING_RE.search(key):
        return (
            "holy_site_control_warning",
            "small_human_review_possible",
            "Single warning label with FAITH.GetName; can be human-reviewed separately but should not be automatic.",
        )
    if COUNT_REQUIREMENT_RE.search(key):
        return (
            "holy_site_count_requirement_trigger",
            "parser_later_requirement_trigger",
            "Trigger text needs quantity/control parser before lifecycle or candidate generation.",
        )
    if EFFECT_NAME_KEY_RE.search(key):
        return (
            "holy_site_effect_name_preposition_line",
            "subpolicy_readonly_or_tiny_human_packet",
            "Highly repetitive 'From holy site name' surface; suitable for read-only subpolicy and possibly tiny human review, not automatic candidate generation yet.",
        )
    return (
        "dense_holy_site_unknown",
        "hold_architecture_later",
        "Dense holy-site token row did not match known subfamilies.",
    )


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def representative_examples(rows: list[dict[str, Any]], limit: int = 5) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        family = str(row["dense_subfamily"])
        if len(grouped[family]) < limit:
            grouped[family].append(
                {
                    "segment_id": row["segment_id"],
                    "source_key": row["source_key"],
                    "relative_path": row["relative_path"],
                    "current_output_text": row["current_output_text"],
                    "english_text": row["english_text"],
                    "recommended_action": row["recommended_action"],
                }
            )
    return dict(grouped)


def main() -> None:
    source_rows = read_jsonl(INPUT_JSONL_PATH)
    dense_rows = [
        row
        for row in source_rows
        if row.get("operational_subfamily") == "dense_token_cluster_hold"
    ]
    if len(dense_rows) != EXPECTED_COUNT:
        raise SystemExit(f"dense count guard failed: {len(dense_rows)} expected {EXPECTED_COUNT}")

    reviewed: list[dict[str, Any]] = []
    for row in sorted(dense_rows, key=lambda item: (str(item.get("source_key") or ""), int(item["segment_id"]))):
        family, action, reason = classify(row)
        reviewed.append(
            {
                "record_type": "holy_site_dense_token_diagnostic",
                "source": SOURCE,
                "input_review_jsonl": str(INPUT_JSONL_PATH),
                "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
                "segment_id": int(row["segment_id"]),
                "source_key": row.get("source_key"),
                "relative_path": row.get("relative_path"),
                "risk_bucket": row.get("risk_bucket"),
                "token_count": int(row.get("token_count") or 0),
                "bracket_token_count": int(row.get("bracket_token_count") or 0),
                "current_output_text": row.get("current_output_text"),
                "english_text": row.get("english_text"),
                "spanish_text": row.get("spanish_text"),
                "latest_state_group": row.get("latest_state_group"),
                "latest_final_state": row.get("latest_final_state"),
                "latest_needs_output_apply": int(row.get("latest_needs_output_apply") or 0),
                "dense_subfamily": family,
                "recommended_action": action,
                "diagnostic_reason": reason,
                "candidate_generation_allowed": False,
                "auto_apply_allowed": False,
                "lifecycle_allowed": False,
                "production_release_allowed": False,
            }
        )

    family_counts = Counter(row["dense_subfamily"] for row in reviewed)
    action_counts = Counter(row["recommended_action"] for row in reviewed)
    risk_counts = Counter(row["risk_bucket"] for row in reviewed)
    token_counts = Counter(str(row["token_count"]) for row in reviewed)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_dense_token_diagnostic",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
        "input_review_jsonl": str(INPUT_JSONL_PATH),
        "diagnostic_count": len(reviewed),
        "expected_count": EXPECTED_COUNT,
        "count_matches_expected": len(reviewed) == EXPECTED_COUNT,
        "dense_subfamily_counts": dict(family_counts),
        "recommended_action_counts": dict(action_counts),
        "risk_bucket_counts": dict(risk_counts),
        "token_count_distribution": dict(token_counts),
        "representative_examples": representative_examples(reviewed),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "single_operational_recommendation": (
            "Create a read-only subpolicy for holy_site_effect_name_preposition_line and review a tiny human sample; "
            "keep controlled-count and requirement-trigger rows parser-later."
        ),
        "output_files": {},
    }

    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_religion_faith_holy_site_dense_token_diagnostic"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    write_jsonl(jsonl_path, reviewed)
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary_json": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    lines = [
        "domain_policy_vote_candidate religion_faith holy-site dense-token diagnostic",
        "",
        f"diagnostic_count: {len(reviewed)}",
        "",
        "dense_subfamily_counts:",
        *[f"- {count} | {key}" for key, count in family_counts.most_common()],
        "",
        "recommended_action_counts:",
        *[f"- {count} | {key}" for key, count in action_counts.most_common()],
        "",
        "representative_examples:",
    ]
    for family, examples in summary["representative_examples"].items():
        lines.extend(["", f"## {family}"])
        for example in examples:
            output = str(example["current_output_text"] or "").replace("\n", "\\n")
            lines.extend(
                [
                    f"- segment_id {example['segment_id']} | {example['source_key']}",
                    f"  action: {example['recommended_action']}",
                    f"  output: {output[:360]}",
                ]
            )
    lines.extend(
        [
            "",
            "guards:",
            "- candidate_generation: not_run",
            "- apply: not_run",
            "- lifecycle: not_run",
            "- segment_state: not_run",
            "- reindex: not_run",
            "- full_production: not_run",
            "",
            f"recommendation: {summary['single_operational_recommendation']}",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
