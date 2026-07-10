from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "release_readiness_ui_tooltips_plain_light_human_packet_v1"
INPUT_JSONL = Path("reports/20260702_141239_087912_release_readiness_post544_diagnostic.jsonl")
LIMIT = 80
EXCLUDED_SEGMENT_IDS = {120831}


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only human review packet for UI/tooltips plain/light release readiness.")
    parser.add_argument("--input-jsonl", type=Path, default=INPUT_JSONL)
    parser.add_argument("--limit", type=int, default=LIMIT)
    parser.add_argument("--exclude-segment-ids", default="")
    parser.add_argument("--release-classes", default="release_blocker,review_before_release")
    return parser.parse_args()


def parse_segment_ids(value: str) -> set[int]:
    if not value.strip():
        return set()
    return {int(part.strip()) for part in value.split(",") if part.strip()}


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


def split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [part for part in str(value).split(",") if part]


def suggested_decision(row: dict[str, Any]) -> str:
    issue_blob = " ".join(split_csv(row.get("issue_families")) + split_csv(row.get("issue_kinds"))).lower()
    if int(row.get("needs_output_apply") or 0) == 1 or row.get("output_differs_from_confirmed"):
        return "corrected_text"
    if "high_issue_auditor" in issue_blob or int(row.get("high_issue_count") or 0) > 0:
        return "needs_more_context"
    if "spanish_residual" in issue_blob or row.get("spanish_residue_visible"):
        return "corrected_text"
    if "gender" in issue_blob or row.get("gender_or_perspective"):
        return "needs_more_context"
    if int(row.get("open_issue_count") or 0) > 0:
        return "approve_already_ok"
    return "approve_already_ok"


def risk_type(row: dict[str, Any]) -> str:
    issue_blob = " ".join(split_csv(row.get("issue_families")) + split_csv(row.get("issue_kinds"))).lower()
    output = str(row.get("output_text") or "").strip()
    if "spanish_residual" in issue_blob or row.get("spanish_residue_visible"):
        return "spanish_residue_visible"
    if "gender" in issue_blob or row.get("gender_or_perspective"):
        return "gender_or_perspective_simple"
    if int(row.get("high_issue_count") or 0) > 0:
        return "high_issue_plain_light"
    if row.get("token_surface") == "plain_text" and len(output.split()) <= 8:
        return "short_label_visible"
    if row.get("token_surface") == "light_token":
        return "short_tooltip_light_token"
    return "plain_text_fluency"


def impact_reason(row: dict[str, Any]) -> str:
    reasons: list[str] = []
    if row.get("release_class") == "release_blocker":
        reasons.append("release_blocker")
    if row.get("release_class") == "review_before_release":
        reasons.append("review_before_release")
    if int(row.get("high_issue_count") or 0) > 0:
        reasons.append("high_issue")
    if int(row.get("open_issue_count") or 0) > 0:
        reasons.append("open_issue")
    if row.get("spanish_residue_visible"):
        reasons.append("possible_visible_spanish_residue")
    if row.get("gender_or_perspective"):
        reasons.append("gender_or_perspective_risk")
    reasons.append("ui_tooltip_short_label_visible")
    return ", ".join(dict.fromkeys(reasons))


def priority_key(row: dict[str, Any]) -> tuple[int, int, int, int]:
    class_score = 2 if row.get("release_class") == "release_blocker" else 1
    high = int(row.get("high_issue_count") or 0)
    open_issues = int(row.get("open_issue_count") or 0)
    impact = int(row.get("impact_score") or 0)
    return (class_score, high, open_issues, impact)


def issue_signature(row: dict[str, Any]) -> str:
    families = ",".join(sorted(split_csv(row.get("issue_families"))))
    kinds = ",".join(sorted(split_csv(row.get("issue_kinds"))))
    return f"{families}|{kinds}"[:220]


def select_balanced(eligible: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    eligible.sort(key=priority_key, reverse=True)
    selected: list[dict[str, Any]] = []
    selected_ids: set[int] = set()
    path_counts: Counter[str] = Counter()
    issue_counts: Counter[str] = Counter()
    risk_counts: Counter[str] = Counter()

    for row in eligible:
        if len(selected) >= limit:
            break
        path = str(row.get("relative_path") or "")
        signature = issue_signature(row)
        risk = risk_type(row)
        if path_counts[path] >= 5:
            continue
        if issue_counts[signature] >= 8:
            continue
        if risk_counts[risk] >= 16:
            continue
        selected.append(row)
        selected_ids.add(int(row["segment_id"]))
        path_counts[path] += 1
        issue_counts[signature] += 1
        risk_counts[risk] += 1

    for row in eligible:
        if len(selected) >= limit:
            break
        segment_id = int(row["segment_id"])
        if segment_id in selected_ids:
            continue
        path = str(row.get("relative_path") or "")
        if path_counts[path] >= 7:
            continue
        selected.append(row)
        selected_ids.add(segment_id)
        path_counts[path] += 1
    return selected


def build(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = read_jsonl(args.input_jsonl)
    excluded_segment_ids = EXCLUDED_SEGMENT_IDS | parse_segment_ids(args.exclude_segment_ids)
    release_classes = set(split_csv(args.release_classes))
    eligible = [
        row
        for row in rows
        if int(row.get("segment_id") or 0) not in excluded_segment_ids
        and row.get("visibility_group") == "ui_tooltips_short_labels"
        and row.get("token_surface") in {"plain_text", "light_token"}
        and row.get("release_class") in release_classes
    ]
    selected = select_balanced(eligible, args.limit)

    records: list[dict[str, Any]] = []
    for index, row in enumerate(selected, 1):
        decision = suggested_decision(row)
        record = {
            "source": SOURCE,
            "record_type": "human_review_packet_item",
            "review_index": index,
            "segment_id": int(row["segment_id"]),
            "relative_path": row.get("relative_path"),
            "source_key": row.get("source_key"),
            "source_text": row.get("spanish_text") or row.get("english_text"),
            "english_text": row.get("english_text"),
            "spanish_text": row.get("spanish_text"),
            "output_text": row.get("output_text"),
            "confirmed_text": row.get("confirmed_text"),
            "release_class": row.get("release_class"),
            "token_surface": row.get("token_surface"),
            "issue_family": row.get("issue_families") or "",
            "issue_kind": row.get("issue_kinds") or "",
            "open_issue_count": int(row.get("open_issue_count") or 0),
            "high_issue_count": int(row.get("high_issue_count") or 0),
            "impact_score": int(row.get("impact_score") or 0),
            "release_impact_reason": impact_reason(row),
            "packet_risk_type": risk_type(row),
            "suggested_human_decision": decision,
            "human_decision_options": [
                "approve_already_ok",
                "corrected_text",
                "needs_more_context",
                "parser_later",
                "reject",
            ],
            "candidate_generation_count": 0,
            "apply_count": 0,
            "lifecycle_count": 0,
            "segment_state_count": 0,
            "reindex_count": 0,
            "production_full_count": 0,
        }
        records.append(record)

    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_human_review_packet",
        "input_jsonl": str(args.input_jsonl),
        "eligible_count": len(eligible),
        "selected_count": len(records),
        "limit": args.limit,
        "excluded_segment_ids": sorted(excluded_segment_ids),
        "excluded_segment_id_count": len(excluded_segment_ids),
        "release_class_counts": dict(Counter(record["release_class"] for record in records).most_common()),
        "risk_type_counts": dict(Counter(record["packet_risk_type"] for record in records).most_common()),
        "issue_family_counts": dict(
            Counter(family for record in records for family in (split_csv(record["issue_family"]) or ["none"])).most_common(50)
        ),
        "issue_kind_counts": dict(
            Counter(kind for record in records for kind in (split_csv(record["issue_kind"]) or ["none"])).most_common(50)
        ),
        "token_surface_counts": dict(Counter(record["token_surface"] for record in records).most_common()),
        "suggested_human_decision_counts": dict(Counter(record["suggested_human_decision"] for record in records).most_common()),
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
            "Review the selected UI/tooltips plain/light packet manually. After decisions are recorded, ingest only confirmed signals; apply protected only for corrected_text items."
        ),
    }
    return records, summary


def markdown_text(records: list[dict[str, Any]], summary: dict[str, Any]) -> str:
    lines = [
        "# UI/tooltips plain-light release review packet",
        "",
        f"- Eligible: {summary['eligible_count']}",
        f"- Selected: {summary['selected_count']}",
        "- Mode: read-only human review",
        "- No candidate generation, apply, lifecycle, segment-state, reindex or production full",
        "",
    ]
    for record in records:
        lines.extend(
            [
                f"## {record['review_index']}. Segment {record['segment_id']}",
                "",
                f"- Path: `{record['relative_path']}`",
                f"- Key: `{record['source_key']}`",
                f"- Release class: `{record['release_class']}`",
                f"- Token surface: `{record['token_surface']}`",
                f"- Issues: `{record['issue_family']}` / `{record['issue_kind']}`",
                f"- High issues: `{record['high_issue_count']}`",
                f"- Risk type: `{record['packet_risk_type']}`",
                f"- Impact: {record['release_impact_reason']}",
                f"- Suggested human decision: `{record['suggested_human_decision']}`",
                "",
                "**Source text**",
                "",
                "```text",
                str(record.get("source_text") or ""),
                "```",
                "",
                "**Output text**",
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
                "Decision: `approve_already_ok | corrected_text | needs_more_context | parser_later | reject`",
                "",
                "Corrected text, if any:",
                "",
                "```text",
                "",
                "```",
                "",
            ]
        )
    return "\n".join(lines)


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_release_readiness_ui_tooltips_plain_light_human_packet"
    md_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {"markdown": str(md_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(markdown_text(records, summary), encoding="utf-8")
    return md_path, jsonl_path, summary_path


def main() -> None:
    args = parse_args()
    records, summary = build(args)
    md_path, jsonl_path, summary_path = write_reports(records, summary)
    print(f"markdown={md_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"eligible_count={summary['eligible_count']}")
    print(f"selected_count={summary['selected_count']}")
    print(f"release_class_counts={summary['release_class_counts']}")
    print(f"token_surface_counts={summary['token_surface_counts']}")
    print(f"suggested_human_decision_counts={summary['suggested_human_decision_counts']}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
