from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import canonical_localization_text


SOURCE = "release_readiness_narrative_strict_roi_human_packet_v1"
INPUT_JSONL = Path("reports/20260703_143411_689000_release_readiness_post544_diagnostic.jsonl")
RUN_ID = 578
LIMIT = 30
EXCLUDE_IDS = {
    120831,
    # Previous narrative packets and held correction packet.
    2023, 2044, 2090, 2111, 2113, 2152, 7508, 7528, 7536, 7537, 7557, 7562,
    30364, 30407, 30464, 30863, 30864, 31045, 31402, 32380, 33718, 37839,
    42001, 42196, 42674, 45276, 45336, 55019, 55344, 65282, 66438, 66439,
    70297, 70298, 70373, 74350, 74351, 76766, 79601, 101424, 102046,
    102719, 104908, 110263, 112693, 112699, 112779, 113374, 117478, 123559,
    124230, 124234, 130189, 132024, 132025, 132026, 133040, 133273,
    48500, 54856, 54888, 67282, 67319, 100383, 104983, 105133, 105383,
    112620, 121588, 121728, 121866, 121868, 124242, 126472, 132509, 132516,
}

BANNED_PATTERNS = [
    "Concept(",
    "Glossary(",
    "SelectLocalization",
    "Select_CString",
    ".Get",
    "MakeScope",
    "ScriptValue",
    "ROOT.",
    "$EFFECT_LIST_BULLET$",
]

SPANISH_MARKERS = [
    " el ",
    " la ",
    " los ",
    " las ",
    " una ",
    " un ",
    " que ",
    " por ",
    " para ",
    " con ",
    " de ",
    " del ",
    " está ",
    " puede ",
    " puedes ",
    " tengo ",
    " aunque ",
    " ya ",
    " no ",
]


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Strict read-only high ROI narrative plain/light human packet.")
    parser.add_argument("--input-jsonl", type=Path, default=INPUT_JSONL)
    parser.add_argument("--release-readiness-run-id", type=int, default=RUN_ID)
    parser.add_argument("--limit", type=int, default=LIMIT)
    parser.add_argument("--exclude-segment-ids", default="")
    return parser.parse_args()


def parse_ids(value: str) -> set[int]:
    if not value.strip():
        return set()
    return {int(part.strip()) for part in value.split(",") if part.strip()}


def read_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with db.project_path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [part for part in str(value).split(",") if part]


def has_banned_surface(row: dict[str, Any]) -> bool:
    blob = "\n".join(str(row.get(key) or "") for key in ("spanish_text", "english_text", "output_text", "confirmed_text", "source_key"))
    return any(pattern in blob for pattern in BANNED_PATTERNS) or "\n" in str(row.get("output_text") or "")


def visible_spanish(row: dict[str, Any]) -> bool:
    text = f" {str(row.get('output_text') or '').lower()} "
    if row.get("spanish_residue_visible"):
        return True
    return any(marker in text for marker in SPANISH_MARKERS)


def word_count(row: dict[str, Any]) -> int:
    return len(re.findall(r"\w+", str(row.get("output_text") or "")))


def suggested_corrected_text(row: dict[str, Any]) -> str:
    # This strict packet only accepts explicit deterministic corrected text already present in data.
    # Current readiness rows do not normally carry this; keep empty unless a future row supplies it.
    return str(row.get("suggested_corrected_text") or row.get("proposed_corrected_text") or "").strip()


def initial_decision(row: dict[str, Any]) -> str:
    suggested = suggested_corrected_text(row)
    if suggested and canonical_localization_text(suggested) != canonical_localization_text(str(row.get("output_text") or "")):
        return "corrected_text"
    if (
        int(row.get("needs_output_apply") or 0) == 0
        and int(row.get("confirmed_matches_output") or 0) == 1
        and not visible_spanish(row)
    ):
        return "approve_already_ok"
    return "needs_more_context"


def eligible(row: dict[str, Any], excluded: set[int]) -> tuple[bool, str]:
    segment_id = int(row.get("segment_id") or 0)
    if segment_id in excluded:
        return False, "excluded_reviewed_or_hold"
    if row.get("visibility_group") != "narrative_events":
        return False, "not_narrative_events"
    if row.get("token_surface") not in {"plain_text", "light_token"}:
        return False, "not_plain_light"
    if row.get("release_class") not in {"release_blocker", "review_before_release"}:
        return False, "not_release_relevant"
    issue_families = set(split_csv(row.get("issue_families")))
    issue_kinds = set(split_csv(row.get("issue_kinds")))
    if "high_issue_auditor" in issue_families:
        return False, "excluded_high_issue_auditor"
    if has_banned_surface(row):
        return False, "excluded_dynamic_structural_surface"
    if visible_spanish(row) and not suggested_corrected_text(row):
        return False, "excluded_spanish_residue_without_deterministic_correction"
    if any("spanish" in kind for kind in issue_kinds) and not suggested_corrected_text(row):
        return False, "excluded_spanish_issue_without_deterministic_correction"
    decision = initial_decision(row)
    if decision == "corrected_text" and not suggested_corrected_text(row):
        return False, "excluded_corrected_text_missing_suggestion"
    if decision not in {"approve_already_ok", "corrected_text"}:
        return False, "excluded_needs_context"
    return True, "eligible"


def priority(row: dict[str, Any]) -> tuple[int, int, int, int]:
    release_rank = 0 if row.get("release_class") == "release_blocker" else 1
    decision_rank = 0 if initial_decision(row) == "approve_already_ok" else 1
    token_rank = 0 if row.get("token_surface") == "plain_text" else 1
    return (release_rank, decision_rank, token_rank, word_count(row), -int(row.get("impact_score") or 0))


def build(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    excluded = EXCLUDE_IDS | parse_ids(args.exclude_segment_ids)
    rows = read_rows(args.input_jsonl)
    reject_counts: Counter[str] = Counter()
    candidates: list[dict[str, Any]] = []
    for row in rows:
        ok, reason = eligible(row, excluded)
        if ok:
            candidates.append(row)
        else:
            reject_counts[reason] += 1
    selected = sorted(candidates, key=priority)[: args.limit]
    records: list[dict[str, Any]] = []
    for index, row in enumerate(selected, 1):
        decision = initial_decision(row)
        record = {
            "source": SOURCE,
            "record_type": "strict_roi_human_review_item",
            "review_index": index,
            "segment_id": int(row["segment_id"]),
            "relative_path": row.get("relative_path"),
            "source_key": row.get("source_key"),
            "release_class": row.get("release_class"),
            "token_surface": row.get("token_surface"),
            "issue_families": row.get("issue_families") or "",
            "issue_kinds": row.get("issue_kinds") or "",
            "open_issue_count": int(row.get("open_issue_count") or 0),
            "high_issue_count": int(row.get("high_issue_count") or 0),
            "source_text": row.get("spanish_text"),
            "english_text": row.get("english_text"),
            "output_text": row.get("output_text"),
            "confirmed_text": row.get("confirmed_text"),
            "suggested_corrected_text": suggested_corrected_text(row),
            "suggested_human_decision": decision,
            "human_decision_options": ["approve_already_ok", "corrected_text", "needs_more_context", "parser_later", "reject"],
            "expected_yield_class": "high" if decision in {"approve_already_ok", "corrected_text"} else "low",
            "candidate_generation_count": 0,
            "apply_count": 0,
            "learning_ingest_count": 0,
            "issue_closure_count": 0,
            "lifecycle_count": 0,
            "segment_state_count": 0,
            "reindex_count": 0,
            "production_full_count": 0,
        }
        records.append(record)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_strict_roi_human_packet",
        "input_jsonl": str(args.input_jsonl),
        "release_readiness_run_id": args.release_readiness_run_id,
        "limit": args.limit,
        "eligible_count": len(candidates),
        "record_count": len(records),
        "excluded_segment_id_count": len(excluded),
        "reject_reason_counts": dict(reject_counts.most_common(20)),
        "release_class_counts": dict(Counter(record["release_class"] for record in records).most_common()),
        "token_surface_counts": dict(Counter(record["token_surface"] for record in records).most_common()),
        "suggested_decision_counts": dict(Counter(record["suggested_human_decision"] for record in records).most_common()),
        "expected_yield": {
            "low": sum(1 for record in records if record["suggested_human_decision"] == "approve_already_ok"),
            "high": len(records),
            "basis": "strict filter: approve_already_ok or deterministic corrected_text only",
        },
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
        "single_operational_recommendation": "Review this strict packet. If empty or low-volume, next ROI requires either filled human corrections or architecture/parser work.",
    }
    return records, summary


def markdown(records: list[dict[str, Any]], summary: dict[str, Any]) -> str:
    lines = [
        "# Narrative strict ROI human packet",
        "",
        f"- eligible_count: {summary['eligible_count']}",
        f"- record_count: {summary['record_count']}",
        f"- release_class_counts: `{json.dumps(summary['release_class_counts'], ensure_ascii=False, sort_keys=True)}`",
        f"- token_surface_counts: `{json.dumps(summary['token_surface_counts'], ensure_ascii=False, sort_keys=True)}`",
        f"- suggested_decision_counts: `{json.dumps(summary['suggested_decision_counts'], ensure_ascii=False, sort_keys=True)}`",
        f"- expected_yield: `{json.dumps(summary['expected_yield'], ensure_ascii=False, sort_keys=True)}`",
        "- No candidate generation, apply, learning ingest, issue closure, lifecycle, segment-state, reindex or production full",
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
                f"- Suggested decision: `{record['suggested_human_decision']}`",
                f"- Issues: `{record['issue_families']}` / `{record['issue_kinds']}`",
                "",
                "**Source ES**",
                "",
                "```text",
                str(record.get("source_text") or ""),
                "```",
                "",
                "**English**",
                "",
                "```text",
                str(record.get("english_text") or ""),
                "```",
                "",
                "**Output**",
                "",
                "```text",
                str(record.get("output_text") or ""),
                "```",
                "",
                "**Confirmed**",
                "",
                "```text",
                str(record.get("confirmed_text") or ""),
                "```",
                "",
                "Human decision: `approve_already_ok | corrected_text | needs_more_context | parser_later | reject`",
                "",
                "Corrected text, only if needed:",
                "",
                "```text",
                str(record.get("suggested_corrected_text") or ""),
                "```",
                "",
            ]
        )
    return "\n".join(lines)


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_release_readiness_narrative_strict_roi_human_packet"
    md_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {"markdown": str(md_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(markdown(records, summary), encoding="utf-8")
    return md_path, jsonl_path, summary_path


def main() -> None:
    args = parse_args()
    records, summary = build(args)
    md_path, jsonl_path, summary_path = write_reports(records, summary)
    print(f"markdown={md_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"eligible_count={summary['eligible_count']}")
    print(f"record_count={summary['record_count']}")
    print(f"suggested_decision_counts={json.dumps(summary['suggested_decision_counts'], ensure_ascii=False, sort_keys=True)}")
    print(f"expected_yield={json.dumps(summary['expected_yield'], ensure_ascii=False, sort_keys=True)}")
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
