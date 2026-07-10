from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "release_decision_post592_report_readonly_v1"
DEFAULT_READINESS_SUMMARY = Path("reports/20260703_225420_617438_release_readiness_post544_diagnostic_summary.json")
DEFAULT_BLOCKER_SUMMARY = Path("reports/20260703_225427_230255_release_readiness_post590_high_issue_summary_readonly_summary.json")
DEFAULT_BLOCKER_JSONL = Path("reports/20260703_225427_230255_release_readiness_post590_high_issue_summary_readonly.jsonl")

SHORT_TEXT_MAX = 160
SPANISH_RE = re.compile(
    r"\b(el|la|los|las|un|una|este|esta|estos|estas|mucho|mucha|muchos|muchas|mayor|menor|"
    r"ganando|aplicarlo|consigue|obtiene|tiene|puede|debe|para|con|sin|que)\b",
    re.IGNORECASE,
)
CONTEXT_RE = re.compile(r"Custom\(|Get|Select_CString|SelectLocalization|ROOT\.|SCOPE\.|\[[^\]]+\]", re.IGNORECASE)


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only post-592 release decision report.")
    parser.add_argument("--readiness-summary", type=Path, default=DEFAULT_READINESS_SUMMARY)
    parser.add_argument("--blocker-summary", type=Path, default=DEFAULT_BLOCKER_SUMMARY)
    parser.add_argument("--blocker-jsonl", type=Path, default=DEFAULT_BLOCKER_JSONL)
    return parser.parse_args()


def load_json(path: Path) -> dict[str, Any]:
    with db.project_path(path).open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with db.project_path(path).open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def visibility_group(relative_path: str) -> str:
    lower = relative_path.lower()
    if "event" in lower:
        return "narrative_events"
    if "tooltip" in lower or "gui" in lower or "interface" in lower:
        return "ui_tooltips_short_labels"
    if "religion" in lower or "faith" in lower:
        return "religion_faith_doctrine"
    if "culture" in lower or "tradition" in lower:
        return "culture_tradition_innovation"
    if "nickname" in lower:
        return "nicknames"
    if "interaction" in lower or "scheme" in lower or "contract" in lower:
        return "interactions_schemes_contracts"
    return "other_visible_or_system"


def file_bucket(relative_path: str) -> str:
    return Path(relative_path).name or relative_path


def corrected_text_assessment(row: dict[str, Any]) -> dict[str, Any]:
    output = str(row.get("output_text") or "")
    confirmed = str(row.get("confirmed_text") or "")
    rel = str(row.get("relative_path") or "")
    visible_group = visibility_group(rel)
    has_context_tokens = bool(CONTEXT_RE.search(output) or CONTEXT_RE.search(confirmed))
    has_spanish = bool(SPANISH_RE.search(output) or SPANISH_RE.search(confirmed))
    is_short = max(len(output), len(confirmed)) <= SHORT_TEXT_MAX
    if is_short and not has_context_tokens:
        fixability = "short_plain_likely_correctable"
    elif is_short and has_context_tokens:
        fixability = "short_light_token_needs_care"
    elif has_context_tokens:
        fixability = "needs_context_token_review"
    else:
        fixability = "medium_text_likely_correctable"
    if "narrative_events" == visible_group:
        visibility = "high_gameplay_narrative"
    elif visible_group in {"ui_tooltips_short_labels", "interactions_schemes_contracts"}:
        visibility = "high_ui_or_interaction"
    elif visible_group in {"religion_faith_doctrine", "culture_tradition_innovation", "nicknames"}:
        visibility = "medium_high_domain_visible"
    else:
        visibility = "medium_or_system_visible"
    return {
        "segment_id": int(row["segment_id"]),
        "relative_path": rel,
        "file_bucket": file_bucket(rel),
        "visibility_group": visible_group,
        "visibility": visibility,
        "source_key": row.get("source_key"),
        "token_surface": row.get("token_surface"),
        "open_issue_count": int(row.get("open_issue_count") or 0),
        "high_issue_count": int(row.get("high_issue_count") or 0),
        "issue_families": row.get("issue_families") or "",
        "issue_kinds": row.get("issue_kinds") or "",
        "output_text": output,
        "confirmed_text": confirmed,
        "has_spanish_residue_signal": has_spanish,
        "has_context_or_token_signal": has_context_tokens,
        "is_short_or_compact": is_short,
        "fixability": fixability,
        "recommended_decision": (
            "review_top_priority_corrected_text"
            if visibility.startswith("high") and fixability in {"short_plain_likely_correctable", "short_light_token_needs_care"}
            else "review_with_context_or_hold"
        ),
    }


def main() -> None:
    args = parse_args()
    readiness = load_json(args.readiness_summary)
    blocker_summary = load_json(args.blocker_summary)
    rows = read_jsonl(args.blocker_jsonl)
    corrected = [corrected_text_assessment(row) for row in rows if row.get("classification") == "corrected_text_needs_human_text"]
    corrected.sort(key=lambda r: (-int(r["high_issue_count"]), r["visibility_group"], r["relative_path"], int(r["segment_id"])))
    corrected_counts = Counter(r["fixability"] for r in corrected)
    corrected_groups = Counter(r["visibility_group"] for r in corrected)
    corrected_files = Counter(r["file_bucket"] for r in corrected)
    corrected_visibility = Counter(r["visibility"] for r in corrected)
    short_correctable = sum(1 for r in corrected if r["fixability"] == "short_plain_likely_correctable")
    needs_context = sum(1 for r in corrected if r["fixability"] in {"short_light_token_needs_care", "needs_context_token_review"})
    top_n = min(15, len(corrected))
    if short_correctable + sum(1 for r in corrected if r["fixability"] == "medium_text_likely_correctable") >= 20:
        recommendation = "review_all_35_before_release"
        recommendation_reason = "The remaining non-dynamic corrected-text blockers are small enough to clear and likely player-visible."
    elif top_n >= 10:
        recommendation = "review_top_n_then_release_decision"
        recommendation_reason = "A focused top-N pass can remove the highest-severity visible residue without opening parser work."
    else:
        recommendation = "publish_with_documented_limitations"
        recommendation_reason = "Corrected-text blockers are below a meaningful manual batch size."
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_release_decision_post592",
        "pending_total": readiness.get("pending_global_count"),
        "release_blocker": readiness.get("release_blocker_count"),
        "review_before_release": readiness.get("review_before_release_count"),
        "known_non_blocking_hold": readiness.get("known_non_blocking_hold_count"),
        "parser_later": readiness.get("parser_later_count"),
        "needs_output_apply": readiness.get("needs_output_apply_count"),
        "needs_output_apply_segment_ids": readiness.get("needs_output_apply_segment_ids", []),
        "blocker_split": {
            "corrected_text_needs_human_text": blocker_summary.get("remaining_high_issue_non_dynamic_counts", {}).get(
                "corrected_text_needs_human_text", 0
            ),
            "parser_later_or_dynamic_excluded": blocker_summary.get("remaining_high_issue_non_dynamic_counts", {}).get(
                "parser_later_or_dynamic_excluded", 0
            ),
            "approve_already_ok_possible": blocker_summary.get("remaining_high_issue_non_dynamic_counts", {}).get(
                "approve_already_ok_possible", 0
            ),
            "other_release_blockers": int(readiness.get("release_blocker_count") or 0)
            - sum(int(v) for v in blocker_summary.get("remaining_high_issue_non_dynamic_counts", {}).values()),
        },
        "corrected_text_analysis": {
            "record_count": len(corrected),
            "group_counts": dict(corrected_groups.most_common()),
            "file_counts": dict(corrected_files.most_common()),
            "visibility_counts": dict(corrected_visibility.most_common()),
            "fixability_counts": dict(corrected_counts.most_common()),
            "short_plain_likely_correctable_count": short_correctable,
            "needs_context_or_token_review_count": needs_context,
            "top_n_recommended_count": top_n,
            "top_n_segment_ids": [r["segment_id"] for r in corrected[:top_n]],
        },
        "release_decision_options": {
            "publish_after_reviewing_35": "recommended" if recommendation == "review_all_35_before_release" else "viable",
            "publish_without_reviewing_35": "viable_with_known_limitations",
            "review_top_n_of_35": "recommended" if recommendation == "review_top_n_then_release_decision" else "viable",
            "open_parser_dynamic_sprint_before_release": "not_recommended_for_current_release",
        },
        "single_operational_recommendation": recommendation,
        "recommendation_reason": recommendation_reason,
        "candidate_generation_count": 0,
        "apply_count": 0,
        "learning_ingest_count": 0,
        "issue_closure_count": 0,
        "lifecycle_count": 0,
        "materializer_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "production_full_recommended_now": False,
        "source_changed": False,
        "output_changed": False,
    }
    base = reports_dir() / f"{stamp()}_release_decision_post592_report_readonly"
    md_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in corrected:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    md_lines = [
        "# Release Decision Post-592",
        "",
        "## Current State",
        f"- pending_total: {summary['pending_total']}",
        f"- release_blocker: {summary['release_blocker']}",
        f"- review_before_release: {summary['review_before_release']}",
        f"- known_non_blocking_hold: {summary['known_non_blocking_hold']}",
        f"- parser_later: {summary['parser_later']}",
        f"- needs_output_apply: {summary['needs_output_apply']} ({summary['needs_output_apply_segment_ids']})",
        "",
        "## Remaining Blockers",
        *[f"- {key}: {value}" for key, value in summary["blocker_split"].items()],
        "",
        "## Corrected Text Needs Human Text",
        f"- count: {len(corrected)}",
        f"- groups: {json.dumps(summary['corrected_text_analysis']['group_counts'], ensure_ascii=False)}",
        f"- fixability: {json.dumps(summary['corrected_text_analysis']['fixability_counts'], ensure_ascii=False)}",
        f"- top_n_recommended_count: {top_n}",
        f"- top_n_segment_ids: {summary['corrected_text_analysis']['top_n_segment_ids']}",
        "",
        "## Recommendation",
        f"- single_operational_recommendation: {recommendation}",
        f"- reason: {recommendation_reason}",
        "",
        "## Top Corrected-Text Items",
    ]
    for row in corrected[:top_n]:
        md_lines.extend(
            [
                f"### {row['segment_id']} - {row['source_key']}",
                f"- file: {row['relative_path']}",
                f"- visibility_group: {row['visibility_group']}",
                f"- high_issue_count: {row['high_issue_count']}",
                f"- fixability: {row['fixability']}",
                f"- output: {row['output_text']}",
                f"- confirmed/current target: {row['confirmed_text']}",
                "",
            ]
        )
    summary["output_files"] = {"markdown": str(md_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    print(f"markdown={md_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"pending_total={summary['pending_total']}")
    print(f"release_blocker={summary['release_blocker']}")
    print(f"blocker_split={json.dumps(summary['blocker_split'], ensure_ascii=False, sort_keys=True)}")
    print(f"corrected_text_analysis={json.dumps(summary['corrected_text_analysis'], ensure_ascii=False, sort_keys=True)}")
    print(f"single_operational_recommendation={recommendation}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("learning_ingest_count=0")
    print("issue_closure_count=0")
    print("lifecycle_count=0")
    print("materializer_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
