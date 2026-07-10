from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "release_readiness_narrative_plain_light_human_packet_v1"
INPUT_JSONL = Path("reports/20260703_003509_315843_release_readiness_post544_diagnostic.jsonl")
RUN_ID = 567
DEFAULT_LIMIT = 40
EXCLUDE_IDS = {120831}

BANNED_SURFACE_PATTERNS = [
    "Concept(",
    "Glossary(",
    "SelectLocalization",
    "Select_CString",
    ".Get",
    "MakeScope",
    "ScriptValue",
    "ROOT.",
    "scope:",
    "$EFFECT_LIST_BULLET$",
]


SPANISH_MARKERS = [
    " esto ",
    " esta ",
    " este ",
    " año",
    " años",
    " mayor",
    " gana ",
    " respeto",
    " sobre ti",
    " terminará",
    " tuvo lugar",
    " presencia",
    " se ",
    " le ",
    " por la ",
    " en tu ",
    " del ",
    " de la ",
]


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only human packet for narrative plain/light release readiness.")
    parser.add_argument("--input-jsonl", type=Path, default=INPUT_JSONL)
    parser.add_argument("--release-readiness-run-id", type=int, default=RUN_ID)
    parser.add_argument("--exclude-segment-ids", default="")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    return parser.parse_args()


def parse_segment_ids(value: str) -> set[int]:
    if not value.strip():
        return set()
    return {int(part.strip()) for part in value.split(",") if part.strip()}


def read_rows(path: Path, excluded_segment_ids: set[int]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with db.project_path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if int(row.get("segment_id") or 0) in excluded_segment_ids:
                continue
            if row.get("visibility_group") != "narrative_events":
                continue
            if row.get("token_surface") not in {"plain_text", "light_token"}:
                continue
            if row.get("release_class") not in {"release_blocker", "review_before_release"}:
                continue
            if row.get("release_class") == "parser_later":
                continue
            text_blob = "\n".join(
                str(row.get(key) or "")
                for key in ("spanish_text", "english_text", "output_text", "confirmed_text", "source_key")
            )
            if any(pattern in text_blob for pattern in BANNED_SURFACE_PATTERNS):
                continue
            rows.append(row)
    return rows


def words(value: str | None) -> int:
    return len(re.findall(r"\w+", value or ""))


def visible_spanish_kind(row: dict[str, Any]) -> str:
    blob = f" {(row.get('output_text') or '').lower()} "
    if any(marker in blob for marker in SPANISH_MARKERS):
        return "visible_spanish_lexical"
    if row.get("spanish_residue_visible"):
        return "spanish_flag_needs_review"
    return "none"


def risk_type(row: dict[str, Any]) -> str:
    if visible_spanish_kind(row) != "none":
        return "spanish_residue_visible"
    if row.get("gender_or_perspective"):
        return "gender_or_perspective_simple"
    if int(row.get("high_issue_count") or 0) > 0:
        return "high_issue_plain_light"
    if row.get("token_surface") == "plain_text" and words(row.get("output_text")) <= 18:
        return "short_plain_fluency"
    if row.get("token_surface") == "light_token":
        return "light_token_fluency"
    return "plain_text_fluency"


def suggested_decision(row: dict[str, Any], risk: str) -> str:
    if risk == "spanish_residue_visible":
        return "corrected_text"
    if risk == "gender_or_perspective_simple":
        return "needs_more_context"
    if row.get("confirmed_matches_output") == 1 and int(row.get("needs_output_apply") or 0) == 0 and int(row.get("open_issue_count") or 0) == 0:
        return "approve_already_ok"
    if row.get("token_surface") in {"plain_text", "light_token"}:
        return "needs_more_context"
    return "parser_later"


def impact_sort_key(row: dict[str, Any]) -> tuple[int, int, int, int]:
    release_rank = 0 if row.get("release_class") == "release_blocker" else 1
    return (
        release_rank,
        -int(row.get("impact_score") or 0),
        -int(row.get("high_issue_count") or 0),
        int(row.get("segment_id") or 0),
    )


def select_balanced(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    for row in rows:
        row["packet_risk_type"] = risk_type(row)
        row["suggested_decision"] = suggested_decision(row, row["packet_risk_type"])
    ordered = sorted(rows, key=impact_sort_key)
    selected: list[dict[str, Any]] = []
    by_path: Counter[str] = Counter()
    by_risk: Counter[str] = Counter()
    by_issue_signature: Counter[str] = Counter()

    # First pass: strict balancing, still release-blocker first by sort order.
    for row in ordered:
        if len(selected) >= limit:
            break
        path = str(row.get("relative_path") or "")
        risk = row["packet_risk_type"]
        signature = ",".join(sorted(str(row.get("issue_kinds") or "").split(",")))[:180]
        if by_path[path] >= 6:
            continue
        if by_risk[risk] >= 18:
            continue
        if by_issue_signature[signature] >= 8:
            continue
        selected.append(row)
        by_path[path] += 1
        by_risk[risk] += 1
        by_issue_signature[signature] += 1

    # Second pass: fill remaining slots without the risk cap, keeping path cap.
    selected_ids = {int(row["segment_id"]) for row in selected}
    for row in ordered:
        if len(selected) >= limit:
            break
        if int(row["segment_id"]) in selected_ids:
            continue
        path = str(row.get("relative_path") or "")
        if by_path[path] >= 8:
            continue
        selected.append(row)
        selected_ids.add(int(row["segment_id"]))
        by_path[path] += 1
    return selected


def compact_record(row: dict[str, Any], review_index: int) -> dict[str, Any]:
    keys = [
        "segment_id",
        "relative_path",
        "source_key",
        "release_class",
        "token_surface",
        "impact_score",
        "final_state",
        "state_group",
        "review_state",
        "confirmation_level",
        "confirmation_label",
        "locked",
        "confirmed_matches_output",
        "needs_output_apply",
        "open_issue_count",
        "high_issue_count",
        "issue_families",
        "issue_kinds",
        "spanish_residue_visible",
        "gender_or_perspective",
        "spanish_text",
        "english_text",
        "output_text",
        "confirmed_text",
    ]
    record = {key: row.get(key) for key in keys}
    record.update(
        {
            "source": SOURCE,
            "record_type": "narrative_plain_light_human_review_item",
            "review_index": review_index,
            "packet_risk_type": row["packet_risk_type"],
            "suggested_decision": row["suggested_decision"],
            "suggested_corrected_text": "",
            "human_decision_options": [
                "approve_already_ok",
                "corrected_text",
                "needs_more_context",
                "parser_later",
                "reject",
            ],
            "candidate_generation_count": 0,
            "apply_count": 0,
            "learning_ingest_count": 0,
            "issue_closure_count": 0,
            "lifecycle_count": 0,
            "segment_state_count": 0,
            "reindex_count": 0,
            "production_full_count": 0,
        }
    )
    return record


def build_summary(
    records: list[dict[str, Any]],
    all_candidates: list[dict[str, Any]],
    limit: int,
    release_readiness_run_id: int,
    input_jsonl: Path,
    excluded_segment_ids: set[int],
) -> dict[str, Any]:
    suggested_counts = Counter(record["suggested_decision"] for record in records)
    risk_counts = Counter(record["packet_risk_type"] for record in records)
    expected_corrected = suggested_counts.get("corrected_text", 0)
    expected_already_ok = suggested_counts.get("approve_already_ok", 0)
    context_penalty = suggested_counts.get("needs_more_context", 0) + suggested_counts.get("parser_later", 0)
    expected_yield_low = max(0, expected_corrected + expected_already_ok - context_penalty)
    expected_yield_high = expected_corrected + expected_already_ok
    return {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_human_review_packet",
        "release_readiness_run_id": release_readiness_run_id,
        "input_jsonl": str(input_jsonl),
        "limit": limit,
        "candidate_pool_count": len(all_candidates),
        "record_count": len(records),
        "release_class_counts": dict(Counter(record["release_class"] for record in records).most_common()),
        "token_surface_counts": dict(Counter(record["token_surface"] for record in records).most_common()),
        "risk_type_counts": dict(risk_counts.most_common()),
        "suggested_decision_counts": dict(suggested_counts.most_common()),
        "expected_yield": {
            "low": expected_yield_low,
            "high": expected_yield_high,
            "basis": "corrected_text + approve_already_ok, discounted by needs_more_context/parser_later in the low estimate",
        },
        "issue_family_counts": dict(
            Counter(
                family
                for record in records
                for family in str(record.get("issue_families") or "none").split(",")
                if family
            ).most_common(20)
        ),
        "relative_path_counts": dict(Counter(record["relative_path"] for record in records).most_common()),
        "release_blocker_count": sum(1 for record in records if record["release_class"] == "release_blocker"),
        "review_before_release_count": sum(1 for record in records if record["release_class"] == "review_before_release"),
        "excluded_segment_ids": sorted(excluded_segment_ids),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "learning_ingest_count": 0,
        "issue_closure_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "single_operational_recommendation": (
            "Review these 40 manually. Only after human decisions, split corrected_text into protected apply and approve_already_ok into learning/lifecycle; keep needs_more_context/parser_later out of apply."
        ),
    }


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_release_readiness_narrative_plain_light_human_packet"
    md_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {"markdown": str(md_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    lines = [
        "# Narrative Events Plain/Light Human Review Packet",
        "",
        f"- release_readiness_run_id: {summary['release_readiness_run_id']}",
        f"- record_count: {summary['record_count']}",
        f"- candidate_pool_count: {summary['candidate_pool_count']}",
        f"- release_class_counts: `{json.dumps(summary['release_class_counts'], ensure_ascii=False, sort_keys=True)}`",
        f"- token_surface_counts: `{json.dumps(summary['token_surface_counts'], ensure_ascii=False, sort_keys=True)}`",
        f"- risk_type_counts: `{json.dumps(summary['risk_type_counts'], ensure_ascii=False, sort_keys=True)}`",
        f"- suggested_decision_counts: `{json.dumps(summary['suggested_decision_counts'], ensure_ascii=False, sort_keys=True)}`",
        "- candidate_generation_count: 0",
        "- apply_count: 0",
        "- learning_ingest_count: 0",
        "- issue_closure_count: 0",
        "- lifecycle_count: 0",
        "- segment_state_count: 0",
        "- reindex_count: 0",
        "- production_full_count: 0",
        "",
    ]
    for record in records:
        lines.extend(
            [
                f"## {record['review_index']}. Segment {record['segment_id']} - {record['source_key']}",
                "",
                f"- path: `{record['relative_path']}`",
                f"- release_class: `{record['release_class']}`",
                f"- token_surface: `{record['token_surface']}`",
                f"- risk_type: `{record['packet_risk_type']}`",
                f"- issues: open={record.get('open_issue_count')} high={record.get('high_issue_count')}",
                f"- issue_families: `{record.get('issue_families') or ''}`",
                f"- issue_kinds: `{record.get('issue_kinds') or ''}`",
                f"- suggested_decision: `{record['suggested_decision']}`",
                "",
                "**Source ES**",
                "",
                "```text",
                str(record.get("spanish_text") or ""),
                "```",
                "",
                "**English**",
                "",
                "```text",
                str(record.get("english_text") or ""),
                "```",
                "",
                "**Current output**",
                "",
                "```text",
                str(record.get("output_text") or ""),
                "```",
                "",
                "**Confirmed text**",
                "",
                "```text",
                str(record.get("confirmed_text") or ""),
                "```",
                "",
                "**Human decision**: `approve_already_ok` / `corrected_text` / `needs_more_context` / `parser_later` / `reject`",
                "",
                "**Corrected text, if needed**:",
                "",
                "```text",
                "",
                "```",
                "",
            ]
        )
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path, jsonl_path, summary_path


def main() -> None:
    args = parse_args()
    excluded_segment_ids = EXCLUDE_IDS | parse_segment_ids(args.exclude_segment_ids)
    rows = read_rows(args.input_jsonl, excluded_segment_ids)
    selected = select_balanced(rows, args.limit)
    records = [compact_record(row, idx + 1) for idx, row in enumerate(selected)]
    summary = build_summary(
        records,
        rows,
        args.limit,
        args.release_readiness_run_id,
        args.input_jsonl,
        excluded_segment_ids,
    )
    md_path, jsonl_path, summary_path = write_reports(records, summary)
    print(f"markdown={md_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"candidate_pool_count={summary['candidate_pool_count']}")
    print(f"record_count={summary['record_count']}")
    print(f"release_class_counts={json.dumps(summary['release_class_counts'], ensure_ascii=False, sort_keys=True)}")
    print(f"token_surface_counts={json.dumps(summary['token_surface_counts'], ensure_ascii=False, sort_keys=True)}")
    print(f"risk_type_counts={json.dumps(summary['risk_type_counts'], ensure_ascii=False, sort_keys=True)}")
    print(f"suggested_decision_counts={json.dumps(summary['suggested_decision_counts'], ensure_ascii=False, sort_keys=True)}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("learning_ingest_count=0")
    print("issue_closure_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
