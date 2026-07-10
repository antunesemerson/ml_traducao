from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "release_quality_token_policy_family_review_readonly_v1"


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only family review for non-nickname token deltas.")
    parser.add_argument("--token-delta-jsonl", type=Path, required=True)
    return parser.parse_args()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    resolved = db.project_path(path)
    rows: list[dict[str, Any]] = []
    with resolved.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def short(value: str | None, limit: int = 160) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def is_multiline(text: str | None) -> bool:
    value = text or ""
    return "\\n" in value or "\n" in value


def has_getter(text: str | None) -> bool:
    value = text or ""
    return ".Get" in value or "GetName" in value or "GetFirstName" in value


def has_local_player(text: str | None) -> bool:
    value = text or ""
    return "IsLocalPlayer" in value or "LocalPlayerString" in value


def has_select(text: str | None) -> bool:
    return "Select_CString" in (text or "")


def has_concept(text: str | None) -> bool:
    return "Concept(" in (text or "")


def has_spanish_helper(text: str | None) -> bool:
    value = text or ""
    return "ES_" in value and ".Custom(" in value


def all_added_are_format_tags(added_tokens: list[str]) -> bool:
    return bool(added_tokens) and all(token.startswith("#") or token == "#!" for token in added_tokens)


def classify(record: dict[str, Any]) -> tuple[str, list[str], bool, str]:
    decision = str(record.get("token_delta_decision") or "")
    output_text = str(record.get("output_text") or "")
    confirmed_text = str(record.get("confirmed_text") or "")
    added_tokens = list(record.get("added_tokens") or [])
    removed_tokens = list(record.get("removed_tokens") or [])
    markers = list(record.get("token_markers") or [])
    flags: list[str] = []

    if is_multiline(output_text) or is_multiline(confirmed_text):
        flags.append("multiline")
    if has_getter(output_text) or has_getter(confirmed_text):
        flags.append("getter")
    if has_local_player(output_text) or has_local_player(confirmed_text):
        flags.append("local_player")
    if has_select(output_text) or has_select(confirmed_text):
        flags.append("select_cstring")
    if has_spanish_helper(output_text) or has_spanish_helper(confirmed_text):
        flags.append("spanish_helper")
    if has_concept(output_text) or has_concept(confirmed_text):
        flags.append("concept")
    if added_tokens and not removed_tokens:
        flags.append("token_add_only")
    if removed_tokens and not added_tokens:
        flags.append("token_remove_only")
    if removed_tokens and added_tokens:
        flags.append("token_replace")
    if all_added_are_format_tags(added_tokens) and not removed_tokens:
        flags.append("format_tag_add_only")

    if decision == "nickname_gender_article_token_policy_review":
        return "skip_nickname_reviewed_separately", flags, False, "Use nickname-specific review."
    if "local_player" in flags:
        return "hold_local_player_perspective_policy", flags, False, "Perspective depends on runtime player context."
    if "concept" in flags:
        return "hold_concept_literal_surface_policy", flags, False, "Concept literal surface needs explicit policy."
    if "multiline" in flags:
        return "hold_multiline_token_policy", flags, False, "Multiline token changes need parser-aware policy."
    if decision == "style_suffix_only_token_delta":
        if "getter" not in flags and "select_cstring" not in flags:
            return "hold_style_suffix_literal_token_policy", flags, False, "Style-suffix literal changes alter token signature; keep for explicit policy."
        return "hold_style_suffix_with_runtime_context", flags, False, "Style change overlaps runtime token."
    if decision == "orthographic_microfix_token_policy_review":
        return "route_orthographic_token_policy_review", flags, False, "Needs dedicated token-aware orthographic review."
    if decision == "manual_feedback_token_policy_review":
        return "route_manual_feedback_token_policy_review", flags, False, "Human feedback needs evidence-specific policy."
    if decision == "spanish_custom_helper_token_policy_review":
        if "token_remove_only" in flags and "select_cstring" not in flags and "getter" not in flags:
            return "route_spanish_helper_removal_candidate", flags, True, "Candidate for ES helper removal policy dry-run."
        return "hold_spanish_helper_runtime_or_select_context", flags, False, "Helper overlaps runtime/select context."
    if decision == "select_cstring_token_policy_review":
        if "getter" in flags:
            return "hold_select_cstring_getter_context", flags, False, "Select_CString overlaps getter."
        return "hold_select_cstring_gender_policy", flags, False, "Select_CString changes require explicit gender/perspective policy."
    if "getter" in flags:
        return "hold_getter_token_policy", flags, False, "Getter token changes need parser policy."
    if "format_tag_add_only" in flags:
        return "route_format_tag_addition_candidate", flags, True, "Candidate for strict format tag policy dry-run."
    if markers:
        return "hold_general_token_policy", flags, False, "Needs token-family policy."
    return "hold_unknown_token_policy", flags, False, "No route matched."


def build_report(token_delta_jsonl: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = read_jsonl(token_delta_jsonl)
    records: list[dict[str, Any]] = []
    for row in rows:
        route, flags, policy_candidate, next_action = classify(row)
        records.append(
            {
                "source": SOURCE,
                "record_type": "token_policy_family_review",
                "segment_state_run_id": row.get("segment_state_run_id"),
                "segment_id": int(row.get("segment_id") or 0),
                "relative_path": row.get("relative_path"),
                "source_key": row.get("source_key"),
                "queue": row.get("queue"),
                "token_delta_decision": row.get("token_delta_decision"),
                "token_markers": row.get("token_markers") or [],
                "route": route,
                "flags": flags,
                "candidate_for_policy_design": policy_candidate,
                "ready_for_apply": False,
                "next_action": next_action,
                "added_tokens": row.get("added_tokens") or [],
                "removed_tokens": row.get("removed_tokens") or [],
                "output_text": row.get("output_text"),
                "confirmed_text": row.get("confirmed_text"),
                "apply_count": 0,
                "ingest_count": 0,
                "segment_state_count": 0,
                "production_full_count": 0,
            }
        )

    route_counts = Counter(record["route"] for record in records)
    decision_counts = Counter(record["token_delta_decision"] for record in records)
    flag_counts = Counter(flag for record in records for flag in record["flags"])
    candidate_count = sum(1 for record in records if record["candidate_for_policy_design"])
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "token_delta_jsonl": str(db.project_path(token_delta_jsonl)),
        "record_count": len(records),
        "candidate_for_policy_design_count": candidate_count,
        "hold_or_review_count": len(records) - candidate_count,
        "ready_for_apply_count": 0,
        "route_counts": route_counts.most_common(),
        "decision_counts": decision_counts.most_common(),
        "flag_counts": flag_counts.most_common(),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "ingest_count": 0,
        "issue_closure_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "single_operational_recommendation": (
            "Only pursue policy dry-runs for routes marked candidate_for_policy_design; all others remain hold/parser/policy review."
        ),
    }
    return records, summary


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_release_quality_token_policy_family_review_readonly"
    md_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "# Release Quality Token Policy Family Review",
        "",
        f"- records: `{summary['record_count']}`",
        f"- candidates for policy design: `{summary['candidate_for_policy_design_count']}`",
        f"- holds/review: `{summary['hold_or_review_count']}`",
        f"- ready for apply now: `{summary['ready_for_apply_count']}`",
        "",
        "## Routes",
        "",
    ]
    for route, count in summary["route_counts"]:
        lines.append(f"- `{route}`: `{count}`")
    lines.extend(["", "## Candidate Samples", ""])
    for record in [item for item in records if item["candidate_for_policy_design"]][:30]:
        lines.extend(
            [
                f"### {record['segment_id']} - {record['route']}",
                f"- decision: `{record['token_delta_decision']}`",
                f"- path: `{record['relative_path']}`",
                f"- key: `{record['source_key']}`",
                f"- added: `{record['added_tokens']}`",
                f"- removed: `{record['removed_tokens']}`",
                f"- output: {short(record['output_text'])}",
                f"- confirmed: {short(record['confirmed_text'])}",
                "",
            ]
        )

    summary["output_files"] = {
        "markdown": str(md_path),
        "jsonl": str(jsonl_path),
        "summary": str(summary_path),
    }
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return md_path, jsonl_path, summary_path


def main() -> None:
    args = parse_args()
    records, summary = build_report(args.token_delta_jsonl)
    md_path, jsonl_path, summary_path = write_reports(records, summary)
    print(f"markdown={md_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"record_count={summary['record_count']}")
    print(f"candidate_for_policy_design_count={summary['candidate_for_policy_design_count']}")
    print(f"hold_or_review_count={summary['hold_or_review_count']}")
    print(f"ready_for_apply_count={summary['ready_for_apply_count']}")
    print(f"route_counts={json.dumps(summary['route_counts'], ensure_ascii=False)}")
    print("apply_count=0")
    print("ingest_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")
    print("source_changed=false")
    print("output_changed=false")


if __name__ == "__main__":
    main()
