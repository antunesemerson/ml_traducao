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


SOURCE = "release_quality_token_delta_review_v1"
STRING_LITERAL_PATTERN = re.compile(r"'[^']*'|\"[^\"]*\"")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only token delta review for blocked priority apply previews.")
    parser.add_argument("--preview-jsonl", type=Path, required=True)
    return parser.parse_args()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    resolved = db.project_path(path)
    rows: list[dict[str, Any]] = []
    with resolved.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def tokens(value: str | None) -> list[str]:
    return TOKEN_PATTERN.findall(value or "")


def bracket_body(token: str) -> str:
    if token.startswith("[") and token.endswith("]"):
        return token[1:-1].strip()
    return token


def token_base(token: str) -> str:
    body = bracket_body(token)
    if "|" in body:
        body = body.rsplit("|", 1)[0].strip()
    body = STRING_LITERAL_PATTERN.sub("'<TEXT>'", body)
    return body


def token_marker(token: str) -> str:
    body = bracket_body(token)
    if "Select_CString" in body:
        return "select_cstring"
    if "LocalPlayerString" in body or "PlayerString" in body:
        return "player_string"
    if "Concept(" in body:
        return "concept"
    if ".Custom(" in body or "Custom(" in body:
        if "ES_" in body:
            return "spanish_custom_helper"
        return "custom_helper"
    if ".Get" in body or body.startswith("Get"):
        return "getter"
    if token.startswith("#") or token == "#!":
        return "format_tag"
    if token.startswith("$"):
        return "variable"
    if token.startswith("@"):
        return "icon"
    if token == "\\n":
        return "escaped_newline"
    return "other_token"


def style_variant_only(output_tokens: list[str], confirmed_tokens: list[str]) -> bool:
    if len(output_tokens) != len(confirmed_tokens):
        return False
    return [token_base(token) for token in output_tokens] == [
        token_base(token) for token in confirmed_tokens
    ]


def classify(record: dict[str, Any]) -> tuple[str, list[str]]:
    output_tokens = tokens(record.get("output_text"))
    confirmed_tokens = tokens(record.get("confirmed_text"))
    removed = list((Counter(output_tokens) - Counter(confirmed_tokens)).elements())
    added = list((Counter(confirmed_tokens) - Counter(output_tokens)).elements())
    markers = sorted({token_marker(token) for token in removed + added})
    queue = str(record.get("queue") or "")
    label = str(record.get("confirmation_label") or "")

    if not removed and not added:
        return "token_signature_same_unexpected_block", markers
    if style_variant_only(output_tokens, confirmed_tokens):
        return "style_suffix_only_token_delta", markers
    if queue == "nickname_visual_apply_review":
        if "spanish_custom_helper" in markers or "select_cstring" in markers:
            return "nickname_gender_article_token_policy_review", markers
        return "nickname_text_token_policy_review", markers
    if "20260613_in_game_feedback" in label:
        return "manual_feedback_token_policy_review", markers
    if "ptbr_orthographic_microfix" in label:
        return "orthographic_microfix_token_policy_review", markers
    if "select_cstring" in markers:
        return "select_cstring_token_policy_review", markers
    if "player_string" in markers:
        return "player_string_token_policy_review", markers
    if "spanish_custom_helper" in markers:
        return "spanish_custom_helper_token_policy_review", markers
    if "getter" in markers:
        return "getter_token_policy_review", markers
    return "general_token_policy_review", markers


def build_report(preview_jsonl: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = read_jsonl(preview_jsonl)
    records: list[dict[str, Any]] = []
    for row in rows:
        output_tokens = tokens(row.get("output_text"))
        confirmed_tokens = tokens(row.get("confirmed_text"))
        removed = list((Counter(output_tokens) - Counter(confirmed_tokens)).elements())
        added = list((Counter(confirmed_tokens) - Counter(output_tokens)).elements())
        decision, markers = classify(row)
        records.append(
            {
                "source": SOURCE,
                "record_type": "token_delta_review",
                "segment_state_run_id": row.get("segment_state_run_id"),
                "segment_id": int(row["segment_id"]),
                "queue": row.get("queue"),
                "token_delta_decision": decision,
                "token_markers": markers,
                "relative_path": row.get("relative_path"),
                "source_key": row.get("source_key"),
                "confirmation_label": row.get("confirmation_label"),
                "output_token_count": len(output_tokens),
                "confirmed_token_count": len(confirmed_tokens),
                "removed_tokens": removed,
                "added_tokens": added,
                "output_text": row.get("output_text"),
                "confirmed_text": row.get("confirmed_text"),
                "ready_for_apply": False,
                "next_action": (
                    "design_or_approve_token_policy_before_apply"
                    if decision.endswith("token_policy_review")
                    else "manual_architecture_review"
                ),
                "apply_count": 0,
                "segment_state_count": 0,
                "production_full_count": 0,
            }
        )
    decision_counts = Counter(record["token_delta_decision"] for record in records)
    marker_counts = Counter(marker for record in records for marker in record["token_markers"])
    queue_counts = Counter(record["queue"] for record in records)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "preview_jsonl": str(db.project_path(preview_jsonl)),
        "record_count": len(records),
        "ready_for_apply_count": 0,
        "blocked_count": len(records),
        "decision_counts": decision_counts.most_common(),
        "marker_counts": marker_counts.most_common(),
        "queue_counts": queue_counts.most_common(),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "single_operational_recommendation": (
            "Do not apply these rows yet. Every row changes CK3 token signature; split by token_delta_decision and approve/design policy first."
        ),
    }
    return records, summary


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_release_quality_token_delta_review"
    md_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    lines = [
        "# Release Quality Token Delta Review",
        "",
        f"- records: `{summary['record_count']}`",
        f"- ready_for_apply: `{summary['ready_for_apply_count']}`",
        f"- blocked: `{summary['blocked_count']}`",
        "",
        "## Decisions",
        "",
    ]
    for decision, count in summary["decision_counts"]:
        lines.append(f"- `{decision}`: `{count}`")
    lines.extend(["", "## Markers", ""])
    for marker, count in summary["marker_counts"]:
        lines.append(f"- `{marker}`: `{count}`")
    lines.extend(["", "## Sample Records", ""])
    for record in records[:40]:
        lines.extend(
            [
                f"### {record['segment_id']} - {record['token_delta_decision']}",
                f"- queue: `{record['queue']}`",
                f"- path: `{record['relative_path']}`",
                f"- key: `{record['source_key']}`",
                f"- removed: `{record['removed_tokens']}`",
                f"- added: `{record['added_tokens']}`",
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
    records, summary = build_report(args.preview_jsonl)
    md_path, jsonl_path, summary_path = write_reports(records, summary)
    print(f"markdown={md_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"record_count={summary['record_count']}")
    print(f"ready_for_apply_count={summary['ready_for_apply_count']}")
    print(f"blocked_count={summary['blocked_count']}")
    print(f"decision_counts={json.dumps(summary['decision_counts'], ensure_ascii=False)}")
    print(f"marker_counts={json.dumps(summary['marker_counts'], ensure_ascii=False)}")
    print("apply_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")
    print("source_changed=false")
    print("output_changed=false")


if __name__ == "__main__":
    main()
