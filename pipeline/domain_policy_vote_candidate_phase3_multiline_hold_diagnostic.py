from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import canonical_localization_text


SOURCE = "domain_policy_vote_candidate_phase3_multiline_hold_diagnostic_v1"
DEFAULT_PHASE3_JSONL = Path("reports/20260702_114424_125868_domain_policy_vote_candidate_phase3_human_misc_closure_diagnostic.jsonl")
TARGET_BUCKET = "multiline_hold_or_later_split"


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only diagnostic for phase3 multiline hold bucket.")
    parser.add_argument("--phase3-jsonl", type=Path, default=DEFAULT_PHASE3_JSONL)
    return parser.parse_args()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with db.project_path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def unescape_l10n(value: str | None) -> str:
    text = str(value or "")
    return text.replace('\\"', '"').replace("\\n", "\n")


def newline_profile(value: str | None) -> str:
    text = str(value or "")
    literal = text.count("\\n")
    actual = text.count("\n")
    if literal and not actual:
        return "literal_backslash_n_only"
    if actual and not literal:
        return "actual_newline_only"
    if actual and literal:
        return "mixed_literal_and_actual_newline"
    return "single_line_no_newline"


def contains_effect_list(text: str) -> bool:
    markers = ["$EFFECT$", "$EFFECT_LIST_BULLET$", "$COSTS$", "#D DEBUG", "#D DEBUG:#!"]
    return any(marker in text for marker in markers)


def contains_ui_block(text: str) -> bool:
    return bool(re.search(r"#T |#!|\$[A-Z0-9_]+\\$|@[A-Za-z0-9_]+!", text))


def classify(row: dict[str, Any]) -> str:
    output_text = str(row.get("output_text") or "")
    confirmed_text = str(row.get("confirmed_text") or "")
    canonical_equal = canonical_localization_text(output_text) == canonical_localization_text(confirmed_text)
    unescaped_equal = unescape_l10n(output_text) == unescape_l10n(confirmed_text)
    combined = f"{output_text}\n{confirmed_text}"
    if canonical_equal or unescaped_equal:
        if contains_effect_list(combined):
            return "serialization_equal_effect_or_debug_hold"
        if contains_ui_block(combined):
            return "serialization_equal_ui_block"
        return "serialization_only_candidate"
    if contains_effect_list(combined):
        return "structural_effect_or_debug_hold"
    if contains_ui_block(combined):
        return "structural_ui_block_hold"
    if newline_profile(output_text) != newline_profile(confirmed_text):
        return "newline_shape_changed_needs_review"
    return "multiline_textual_difference_needs_review"


def compact_sample(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "segment_id": row.get("segment_id"),
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "confirmation_source": row.get("confirmation_source"),
        "confirmation_label": row.get("confirmation_label"),
        "token_surface": row.get("token_surface"),
        "output_profile": newline_profile(row.get("output_text")),
        "confirmed_profile": newline_profile(row.get("confirmed_text")),
        "output_text": row.get("output_text"),
        "confirmed_text": row.get("confirmed_text"),
    }


def build(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = [
        row
        for row in read_jsonl(args.phase3_jsonl)
        if row.get("record_type") == "phase3_human_misc_row" and row.get("operational_bucket") == TARGET_BUCKET
    ]
    records: list[dict[str, Any]] = []
    for row in rows:
        output_text = str(row.get("output_text") or "")
        confirmed_text = str(row.get("confirmed_text") or "")
        family = classify(row)
        canonical_equal = canonical_localization_text(output_text) == canonical_localization_text(confirmed_text)
        unescaped_equal = unescape_l10n(output_text) == unescape_l10n(confirmed_text)
        records.append(
            {
                "source": SOURCE,
                "record_type": "phase3_multiline_hold_row",
                "segment_id": int(row["segment_id"]),
                "relative_path": row.get("relative_path"),
                "source_key": row.get("source_key"),
                "confirmation_level": row.get("confirmation_level"),
                "confirmation_source": row.get("confirmation_source"),
                "confirmation_label": row.get("confirmation_label"),
                "locked": int(row.get("locked") or 0),
                "token_surface": row.get("token_surface"),
                "classification": family,
                "canonical_l10n_equal": int(canonical_equal),
                "unescaped_text_equal": int(unescaped_equal),
                "output_newline_profile": newline_profile(output_text),
                "confirmed_newline_profile": newline_profile(confirmed_text),
                "has_effect_or_debug_marker": int(contains_effect_list(f"{output_text}\n{confirmed_text}")),
                "has_ui_block_marker": int(contains_ui_block(f"{output_text}\n{confirmed_text}")),
                "bridge_candidate_later": family in {"serialization_only_candidate", "serialization_equal_ui_block"},
                "hold_or_architecture_later": family
                not in {"serialization_only_candidate", "serialization_equal_ui_block"},
                "output_text": output_text,
                "confirmed_text": confirmed_text,
                "candidate_generation_count": 0,
                "apply_count": 0,
                "lifecycle_count": 0,
                "segment_state_count": 0,
                "reindex_count": 0,
                "production_full_count": 0,
            }
        )

    classification_counts = Counter(record["classification"] for record in records)
    profile_counts = Counter(
        f"{record['output_newline_profile']} -> {record['confirmed_newline_profile']}" for record in records
    )
    label_counts = Counter(str(record["confirmation_label"]) for record in records)
    source_counts = Counter(str(record["confirmation_source"]) for record in records)
    path_counts = Counter(str(record["relative_path"]).split("/", 1)[0] for record in records)
    samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        if len(samples[record["classification"]]) < 8:
            samples[record["classification"]].append(compact_sample(record))

    bridge_candidate_count = sum(1 for record in records if record["bridge_candidate_later"])
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_phase3_multiline_hold_diagnostic",
        "input_phase3_jsonl": str(args.phase3_jsonl),
        "target_bucket": TARGET_BUCKET,
        "record_count": len(records),
        "bridge_candidate_later_count": bridge_candidate_count,
        "hold_or_architecture_later_count": len(records) - bridge_candidate_count,
        "classification_counts": dict(classification_counts.most_common()),
        "newline_profile_transition_counts": dict(profile_counts.most_common()),
        "confirmation_label_counts": dict(label_counts.most_common(40)),
        "confirmation_source_counts": dict(source_counts.most_common(40)),
        "path_group_counts_top": [{"key": key, "count": count} for key, count in path_counts.most_common(20)],
        "samples_by_classification": dict(samples),
        "bridge_candidate_segment_ids": [record["segment_id"] for record in records if record["bridge_candidate_later"]],
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
    }
    if bridge_candidate_count:
        summary["single_operational_recommendation"] = (
            "Create a separate dry-run for serialization-only multiline closure candidates, excluding effect/debug and structural textual-difference buckets."
        )
    else:
        summary["single_operational_recommendation"] = (
            "No safe serialization-only multiline bridge is visible; keep this bucket in hold/architecture split."
        )
    return records, summary


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_phase3_multiline_hold_diagnostic"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "Phase3 multiline hold diagnostic",
        f"record_count={summary['record_count']}",
        f"bridge_candidate_later_count={summary['bridge_candidate_later_count']}",
        f"hold_or_architecture_later_count={summary['hold_or_architecture_later_count']}",
        f"classification_counts={json.dumps(summary['classification_counts'], ensure_ascii=False, sort_keys=True)}",
        f"newline_profile_transition_counts={json.dumps(summary['newline_profile_transition_counts'], ensure_ascii=False, sort_keys=True)}",
        "",
        "Recommendation:",
        summary["single_operational_recommendation"],
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    args = parse_args()
    records, summary = build(args)
    txt_path, jsonl_path, summary_path = write_reports(records, summary)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"record_count={summary['record_count']}")
    print(f"bridge_candidate_later_count={summary['bridge_candidate_later_count']}")
    print(f"hold_or_architecture_later_count={summary['hold_or_architecture_later_count']}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
