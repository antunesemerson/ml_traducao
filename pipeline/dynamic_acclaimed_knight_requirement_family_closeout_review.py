from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


REPAIR_APPLIED_IDS = {604, 640, 644, 654, 657, 661, 662}
STYLE_WATCH_ID = 601
TARGET_DECISIONS = {
    "acclaimed_knight_requirement_needs_semantic_review",
    "acclaimed_knight_requirement_needs_concept_condition_policy",
    "acclaimed_knight_requirement_needs_script_value_policy",
    "acclaimed_knight_requirement_needs_activity_policy",
}
DECISION_MAP = {
    "acclaimed_knight_requirement_needs_semantic_review": "needs_semantic_microrepair_policy",
    "acclaimed_knight_requirement_needs_concept_condition_policy": "needs_concept_condition_policy",
    "acclaimed_knight_requirement_needs_script_value_policy": "needs_script_value_policy",
    "acclaimed_knight_requirement_needs_activity_policy": "needs_activity_policy",
}
SUBPOLICY_MAP = {
    "acclaimed_knight_requirement_needs_semantic_review": "semantic_review",
    "acclaimed_knight_requirement_needs_concept_condition_policy": "concept_condition",
    "acclaimed_knight_requirement_needs_script_value_policy": "script_value",
    "acclaimed_knight_requirement_needs_activity_policy": "activity_condition",
}


def as_text(value: Any) -> str:
    return "" if value is None else str(value)


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_dynamic_acclaimed_knight_requirement_family_closeout_review"
    return base.with_suffix(".jsonl"), base.with_suffix(".txt")


def suggested_rewrite_for(row: dict[str, Any]) -> str:
    text = as_text(row.get("current_text"))
    segment_id = int(row.get("segment_id") or 0)
    if segment_id in {598, 642, 651, 659, 667} and "car" in text:
        return text.replace("tem um carÃ¡ter", "tem o traÃ§o").replace("tem uma caracterÃ­stica", "tem o traÃ§o")
    return ""


def load_closeout_rows(review_jsonl: Path) -> list[dict[str, Any]]:
    source_rows = [json.loads(line) for line in review_jsonl.read_text(encoding="utf-8").splitlines() if line.strip()]
    output_rows: list[dict[str, Any]] = []
    for row in source_rows:
        segment_id = int(row.get("segment_id") or 0)
        previous_decision = as_text(row.get("decision"))
        if previous_decision not in TARGET_DECISIONS:
            continue
        if segment_id in REPAIR_APPLIED_IDS or segment_id == STYLE_WATCH_ID:
            continue
        closeout_decision = DECISION_MAP.get(previous_decision, "blocked_uncertain")
        subpolicy = as_text(row.get("blocked_reason")) or SUBPOLICY_MAP.get(previous_decision, "blocked_uncertain")
        output_rows.append(
            {
                "segment_id": segment_id,
                "source_key": as_text(row.get("source_key")),
                "relative_path": as_text(row.get("relative_path")),
                "previous_decision": previous_decision,
                "closeout_decision": closeout_decision,
                "subpolicy": subpolicy,
                "current_text": as_text(row.get("current_text")),
                "english_text": as_text(row.get("english_text")),
                "suggested_rewrite": suggested_rewrite_for(row),
                "requires_apply_later": False,
                "lifecycle_candidate": False,
                "risk_flags": row.get("risk_flags") or [],
                "rationale": as_text(row.get("rationale")) or "preserved for later policy-specific review",
            }
        )
    return sorted(output_rows, key=lambda item: int(item["segment_id"]))


def validate(rows: list[dict[str, Any]]) -> None:
    errors: list[str] = []
    if len(rows) != 12:
        errors.append(f"expected_12_rows_got_{len(rows)}")
    ids = {int(row["segment_id"]) for row in rows}
    overlap = ids & REPAIR_APPLIED_IDS
    if overlap:
        errors.append(f"applied_ids_present:{sorted(overlap)}")
    if STYLE_WATCH_ID in ids:
        errors.append("style_watch_601_present")
    apply_later = [row["segment_id"] for row in rows if row.get("requires_apply_later") is True]
    if apply_later:
        errors.append(f"requires_apply_later_true:{apply_later}")
    missing_decisions = [
        row["segment_id"]
        for row in rows
        if row.get("closeout_decision")
        not in {
            "needs_semantic_microrepair_policy",
            "needs_concept_condition_policy",
            "needs_script_value_policy",
            "needs_activity_policy",
            "needs_acclaimed_knight_domain_policy",
            "blocked_uncertain",
        }
    ]
    if missing_decisions:
        errors.append(f"invalid_closeout_decision:{missing_decisions}")
    if errors:
        raise RuntimeError("; ".join(errors))


def write_reports(settings: dict[str, Any], *, review_jsonl: Path, rows: list[dict[str, Any]]) -> tuple[Path, Path]:
    jsonl_path, txt_path = report_paths(settings)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    by_decision = Counter(row["closeout_decision"] for row in rows)
    by_subpolicy = Counter(row["subpolicy"] for row in rows)
    ids_by_subpolicy: dict[str, list[int]] = defaultdict(list)
    for row in rows:
        ids_by_subpolicy[row["subpolicy"]].append(int(row["segment_id"]))

    lines = [
        "Dynamic acclaimed knight requirement family closeout review",
        f"Review JSONL: {review_jsonl}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Rows: {len(rows)}",
        "",
        "Counts by closeout_decision:",
    ]
    for key, value in sorted(by_decision.items()):
        lines.append(f"- {key}: {value}")
    lines.extend(["", "Counts by subpolicy:"])
    for key, value in sorted(by_subpolicy.items()):
        lines.append(f"- {key}: {value}")
    lines.extend(["", "IDs by subpolicy:"])
    for key, values in sorted(ids_by_subpolicy.items()):
        lines.append(f"- {key}: {', '.join(str(value) for value in sorted(values))}")
    lines.extend(
        [
            "",
            "Recommendation:",
            "- Preserve these 12 items for a later policy-specific checkpoint after Git checkpointing.",
            "- Do not apply rewrites from this report directly; suggested_rewrite is diagnostic only.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return jsonl_path, txt_path


def run(*, review_jsonl: Path) -> tuple[list[dict[str, Any]], tuple[Path, Path]]:
    settings = db.load_settings()
    rows = load_closeout_rows(review_jsonl)
    validate(rows)
    paths = write_reports(settings, review_jsonl=review_jsonl, rows=rows)
    return rows, paths


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--review-jsonl", required=True, type=Path)
    args = parser.parse_args()
    rows, (jsonl_path, txt_path) = run(review_jsonl=args.review_jsonl)
    print("[dynamic_acclaimed_knight_requirement_family_closeout_review] Completed")
    print(f"rows={len(rows)}")
    print(f"jsonl={jsonl_path}")
    print(f"txt={txt_path}")


if __name__ == "__main__":
    main()
