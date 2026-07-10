from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import TOKEN_PATTERN


SOURCE = "release_quality_nickname_article_policy_review_readonly_v1"
NICKNAMES_PATH = "nicknames_l_spanish.yml"

SELECT_ARTICLE_RE = re.compile(
    r"\[Select_CString\(\s*CHARACTER\.IsFemale\s*,\s*'a'\s*,\s*'o'\s*\)\]"
)
SELECT_GENDERED_RE = re.compile(r"\[Select_CString\(\s*CHARACTER\.IsFemale\s*,")
ES_SUFFIX_RE = re.compile(r"\[CHARACTER\.Custom\('ES_(?:OA|XA)'\)\]")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only policy split for nickname article/gender token deltas."
    )
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


def short(value: str | None, limit: int = 180) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def token_count(value: str | None) -> int:
    return len(TOKEN_PATTERN.findall(value or ""))


def has_local_player(value: str | None) -> bool:
    return "IsLocalPlayer" in (value or "") or "LocalPlayerString" in (value or "")


def has_runtime_getter(value: str | None) -> bool:
    text = value or ""
    return ".Get" in text or "GetName" in text or "GetFirstName" in text


def text_has_select_article(value: str | None) -> bool:
    return bool(SELECT_ARTICLE_RE.search(value or ""))


def text_has_gender_select(value: str | None) -> bool:
    return bool(SELECT_GENDERED_RE.search(value or ""))


def text_has_es_suffix(value: str | None) -> bool:
    return bool(ES_SUFFIX_RE.search(value or ""))


def is_nickname_description(source_key: str) -> bool:
    return source_key.endswith("_desc") or source_key.endswith(".desc") or source_key.endswith("_description")


def visible_article_pattern(value: str | None) -> bool:
    text = value or ""
    lowered = text.lower()
    return (
        lowered.startswith("el/la ")
        or lowered.startswith("el ")
        or lowered.startswith("la ")
        or lowered.startswith("o/a ")
        or lowered.startswith("o ")
        or lowered.startswith("a ")
    )


def classify(record: dict[str, Any]) -> tuple[str, list[str], bool, str]:
    segment_id = int(record.get("segment_id") or 0)
    source_key = str(record.get("source_key") or "")
    label = str(record.get("confirmation_label") or "")
    output_text = str(record.get("output_text") or "")
    confirmed_text = str(record.get("confirmed_text") or "")
    added_tokens = list(record.get("added_tokens") or [])
    removed_tokens = list(record.get("removed_tokens") or [])
    flags: list[str] = []

    if str(record.get("relative_path") or "") != NICKNAMES_PATH:
        flags.append("unexpected_path")
    if is_nickname_description(source_key):
        flags.append("description_key")
    if "held_exception" in label:
        flags.append("held_exception_label")
    if "gago" in source_key.lower() or "gago" in label.lower():
        flags.append("gago_specific")
    if has_local_player(output_text) or has_local_player(confirmed_text):
        flags.append("local_player_or_perspective")
    if has_runtime_getter(output_text) or has_runtime_getter(confirmed_text):
        flags.append("runtime_getter")
    if text_has_select_article(confirmed_text):
        flags.append("confirmed_select_article")
    if text_has_gender_select(confirmed_text):
        flags.append("confirmed_gender_select")
    if text_has_es_suffix(confirmed_text):
        flags.append("confirmed_es_suffix")
    if text_has_es_suffix(output_text):
        flags.append("output_es_suffix")
    if visible_article_pattern(output_text):
        flags.append("output_visible_article")
    if visible_article_pattern(confirmed_text):
        flags.append("confirmed_visible_article")
    if added_tokens and not removed_tokens:
        flags.append("token_add_only")
    if removed_tokens:
        flags.append("token_removal_present")

    if "unexpected_path" in flags:
        return "hold_unexpected_path", flags, False, "Keep outside nickname policy."
    if "description_key" in flags:
        return "hold_nickname_description_context", flags, False, "Descriptions can carry actor/perspective semantics."
    if "held_exception_label" in flags:
        return "hold_structural_watch_exception", flags, False, "Previously held exception; keep explicit review."
    if "local_player_or_perspective" in flags:
        return "hold_perspective_or_local_player_context", flags, False, "Perspective token changes need separate policy."
    if "runtime_getter" in flags:
        return "hold_runtime_getter_context", flags, False, "Runtime getter overlap needs context policy."
    if "gago_specific" in flags:
        return "route_gago_specific_review", flags, False, "Gago has a known bespoke visual fix path."
    if "confirmed_select_article" in flags and "confirmed_es_suffix" in flags:
        if "token_add_only" in flags or "output_visible_article" in flags or "output_es_suffix" in flags:
            return (
                "route_short_nickname_article_gender_candidate",
                flags,
                True,
                "Candidate for strict nickname article/gender token policy dry-run.",
            )
    if "confirmed_gender_select" in flags:
        return "route_short_nickname_gender_select_review", flags, False, "Gendered Select_CString exists but not the strict article+suffix pattern."
    if "confirmed_es_suffix" in flags:
        return "route_short_nickname_suffix_review", flags, False, "Suffix helper exists without strict article policy shape."
    return "hold_nickname_token_policy_unknown", flags, False, "No strict safe route matched."


def build_report(token_delta_jsonl: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = [
        row
        for row in read_jsonl(token_delta_jsonl)
        if row.get("token_delta_decision") == "nickname_gender_article_token_policy_review"
    ]
    records: list[dict[str, Any]] = []
    for row in rows:
        route, flags, policy_candidate, next_action = classify(row)
        records.append(
            {
                "source": SOURCE,
                "record_type": "nickname_article_policy_review",
                "segment_state_run_id": row.get("segment_state_run_id"),
                "segment_id": int(row.get("segment_id") or 0),
                "relative_path": row.get("relative_path"),
                "source_key": row.get("source_key"),
                "confirmation_label": row.get("confirmation_label"),
                "route": route,
                "flags": flags,
                "candidate_for_policy_design": policy_candidate,
                "ready_for_apply": False,
                "next_action": next_action,
                "output_token_count": token_count(row.get("output_text")),
                "confirmed_token_count": token_count(row.get("confirmed_text")),
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
    flag_counts = Counter(flag for record in records for flag in record["flags"])
    label_counts = Counter(record["confirmation_label"] for record in records)
    candidate_count = sum(1 for record in records if record["candidate_for_policy_design"])
    hold_count = len(records) - candidate_count
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "token_delta_jsonl": str(db.project_path(token_delta_jsonl)),
        "record_count": len(records),
        "candidate_for_policy_design_count": candidate_count,
        "hold_or_review_count": hold_count,
        "ready_for_apply_count": 0,
        "route_counts": route_counts.most_common(),
        "flag_counts": flag_counts.most_common(),
        "confirmation_label_counts": label_counts.most_common(),
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
            "Do not apply yet. If the short nickname article/gender route is large and clean, create a strict dry-run policy for that route only; keep descriptions/getters/perspective/exceptions on hold."
        ),
    }
    return records, summary


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_release_quality_nickname_article_policy_review_readonly"
    md_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "# Release Quality Nickname Article Policy Review",
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
    lines.extend(["", "## Flags", ""])
    for flag, count in summary["flag_counts"]:
        lines.append(f"- `{flag}`: `{count}`")
    lines.extend(["", "## Candidate Samples", ""])
    for record in [item for item in records if item["candidate_for_policy_design"]][:25]:
        lines.extend(
            [
                f"### {record['segment_id']} - {record['source_key']}",
                f"- label: `{record['confirmation_label']}`",
                f"- flags: `{', '.join(record['flags'])}`",
                f"- removed: `{record['removed_tokens']}`",
                f"- added: `{record['added_tokens']}`",
                f"- output: {short(record['output_text'])}",
                f"- confirmed: {short(record['confirmed_text'])}",
                "",
            ]
        )
    lines.extend(["", "## Hold Samples", ""])
    for record in [item for item in records if not item["candidate_for_policy_design"]][:25]:
        lines.extend(
            [
                f"### {record['segment_id']} - {record['route']}",
                f"- key: `{record['source_key']}`",
                f"- flags: `{', '.join(record['flags'])}`",
                f"- next: {record['next_action']}",
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
    print(f"flag_counts={json.dumps(summary['flag_counts'], ensure_ascii=False)}")
    print("apply_count=0")
    print("ingest_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")
    print("source_changed=false")
    print("output_changed=false")


if __name__ == "__main__":
    main()
