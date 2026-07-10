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


SOURCE = "release_readiness_narrative_post580_strict_roi_human_packet_v1"
INPUT_JSONL = Path("reports/20260703_152505_293246_release_readiness_post544_diagnostic.jsonl")
RUN_ID = 580
LIMIT = 30
EXPLICIT_HOLDS = {120831, 126552}
PREVIOUS_NARRATIVE_PACKETS = [
    Path("reports/20260703_005406_073466_release_readiness_narrative_plain_light_human_packet.jsonl"),
    Path("reports/20260703_021344_772801_release_readiness_narrative_plain_light_human_packet.jsonl"),
    Path("reports/20260703_143622_675310_release_readiness_narrative_plain_light_human_packet.jsonl"),
    Path("reports/20260703_143645_067441_release_readiness_narrative_plain_light_human_packet.jsonl"),
    Path("reports/20260703_144234_522671_release_readiness_narrative_post578_correction_human_packet.jsonl"),
    Path("reports/20260703_144255_182813_release_readiness_narrative_post578_correction_human_packet.jsonl"),
    Path("reports/20260703_145023_784855_release_readiness_narrative_strict_roi_human_packet.jsonl"),
]

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
    " del ",
    " puede ",
    " puedes ",
    " tengo ",
    " aunque ",
]


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Post-run 580 strict ROI narrative plain/light human packet.")
    parser.add_argument("--input-jsonl", type=Path, default=INPUT_JSONL)
    parser.add_argument("--release-readiness-run-id", type=int, default=RUN_ID)
    parser.add_argument("--limit", type=int, default=LIMIT)
    return parser.parse_args()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    resolved = db.project_path(path)
    if not resolved.exists():
        return []
    rows: list[dict[str, Any]] = []
    with resolved.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def previous_reviewed_ids() -> set[int]:
    ids = set(EXPLICIT_HOLDS)
    for packet in PREVIOUS_NARRATIVE_PACKETS:
        for row in read_jsonl(packet):
            if row.get("segment_id"):
                ids.add(int(row["segment_id"]))
    return ids


def split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in str(value).split(",") if part.strip()]


def has_banned_surface(row: dict[str, Any]) -> bool:
    blob = "\n".join(
        str(row.get(key) or "")
        for key in ("spanish_text", "english_text", "output_text", "confirmed_text", "source_key")
    )
    return any(pattern in blob for pattern in BANNED_PATTERNS)


def visible_spanish(row: dict[str, Any]) -> bool:
    text = f" {str(row.get('output_text') or '').lower()} "
    if row.get("spanish_residue_visible"):
        return True
    return any(marker in text for marker in SPANISH_MARKERS)


def ambiguous_actor_or_pronoun(row: dict[str, Any]) -> bool:
    blob = " ".join(
        str(row.get(key) or "")
        for key in ("issue_families", "issue_kinds", "spanish_text", "english_text", "output_text")
    ).lower()
    return any(marker in blob for marker in ["pronoun", "perspective", "actor", "scope", "relationtome"])


def suggested_corrected_text(row: dict[str, Any]) -> str:
    return str(row.get("suggested_corrected_text") or row.get("proposed_corrected_text") or "").strip()


def canonical_equal(left: str | None, right: str | None) -> bool:
    return canonical_localization_text(left or "") == canonical_localization_text(right or "")


def word_count(row: dict[str, Any]) -> int:
    return len(re.findall(r"\w+", str(row.get("output_text") or "")))


def classify(row: dict[str, Any], excluded: set[int]) -> tuple[str, str]:
    segment_id = int(row.get("segment_id") or 0)
    if segment_id in excluded:
        return "reject", "already_reviewed_or_explicit_hold"
    if row.get("visibility_group") != "narrative_events":
        return "reject", "not_narrative_events"
    if row.get("token_surface") not in {"plain_text", "light_token"}:
        return "parser_later", "not_plain_light"
    if row.get("release_class") not in {"release_blocker", "review_before_release"}:
        return "reject", "not_release_relevant"
    issue_families = set(split_csv(row.get("issue_families")))
    if "high_issue_auditor" in issue_families:
        return "reject", "high_issue_auditor"
    if has_banned_surface(row):
        return "parser_later", "concept_glossary_select_getter_or_structural_surface"
    if "\n" in str(row.get("output_text") or "") or "\r" in str(row.get("output_text") or ""):
        return "parser_later", "multiline"
    if ambiguous_actor_or_pronoun(row):
        return "needs_more_context", "ambiguous_actor_or_pronoun"

    corrected = suggested_corrected_text(row)
    output = str(row.get("output_text") or "")
    confirmed = str(row.get("confirmed_text") or "")
    if corrected:
        if canonical_equal(corrected, output):
            return "reject", "corrected_text_no_output_change"
        return "corrected_text_ready", "deterministic_corrected_text_present"

    if visible_spanish(row):
        return "needs_more_context", "spanish_residue_without_deterministic_correction"
    if int(row.get("needs_output_apply") or 0) != 0:
        return "needs_more_context", "needs_output_apply"
    if not canonical_equal(output, confirmed):
        return "needs_more_context", "output_confirmed_mismatch"
    if int(row.get("confirmed_matches_output") or 0) != 1:
        return "needs_more_context", "state_not_confirmed_matches_output"
    return "approve_already_ok_ready", "output_already_good"


def priority(row: dict[str, Any], decision: str) -> tuple[int, int, int, int, int]:
    release_rank = 0 if row.get("release_class") == "release_blocker" else 1
    decision_rank = 0 if decision == "approve_already_ok_ready" else 1
    token_rank = 0 if row.get("token_surface") == "plain_text" else 1
    return (release_rank, decision_rank, token_rank, word_count(row), -int(row.get("impact_score") or 0))


def build(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    excluded = previous_reviewed_ids()
    rows = read_jsonl(args.input_jsonl)
    classified_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    eligible: list[tuple[dict[str, Any], str, str]] = []
    for row in rows:
        decision, reason = classify(row, excluded)
        classified_counts[decision] += 1
        reason_counts[reason] += 1
        if decision in {"approve_already_ok_ready", "corrected_text_ready"}:
            eligible.append((row, decision, reason))

    selected = sorted(eligible, key=lambda item: priority(item[0], item[1]))[: args.limit]
    records: list[dict[str, Any]] = []
    for index, (row, decision, reason) in enumerate(selected, 1):
        records.append(
            {
                "source": SOURCE,
                "record_type": "post580_strict_roi_human_review_item",
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
                "selection_reason": reason,
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

    decision_counts = Counter(record["suggested_human_decision"] for record in records)
    expected_yield = {
        "approve_already_ok_ready": decision_counts.get("approve_already_ok_ready", 0),
        "corrected_text_ready": decision_counts.get("corrected_text_ready", 0),
        "needs_more_context": 0,
        "parser_later": 0,
        "reject": 0,
        "total_expected_ready": decision_counts.get("approve_already_ok_ready", 0)
        + decision_counts.get("corrected_text_ready", 0),
        "basis": "corrected_text counts only when suggested_corrected_text is filled and differs from output",
    }
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_post580_strict_roi_human_packet",
        "input_jsonl": str(args.input_jsonl),
        "release_readiness_run_id": args.release_readiness_run_id,
        "limit": args.limit,
        "excluded_segment_id_count": len(excluded),
        "eligible_ready_count": len(eligible),
        "record_count": len(records),
        "classified_decision_counts_all_rows": dict(classified_counts.most_common()),
        "classification_reason_counts_all_rows": dict(reason_counts.most_common(30)),
        "release_class_counts": dict(Counter(record["release_class"] for record in records).most_common()),
        "token_surface_counts": dict(Counter(record["token_surface"] for record in records).most_common()),
        "suggested_decision_counts": dict(decision_counts.most_common()),
        "expected_yield": expected_yield,
        "excluded_holds": sorted(EXPLICIT_HOLDS),
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
            "Review this read-only packet. If approved, validate approve_already_ok rows before ingest; corrected_text rows require diff preview first."
        ),
    }
    return records, summary


def markdown(records: list[dict[str, Any]], summary: dict[str, Any]) -> str:
    lines = [
        "# Narrative Events Strict ROI Packet Pós-Run 580",
        "",
        f"- Run: `{summary['release_readiness_run_id']}`",
        f"- Eligible ready before limit: `{summary['eligible_ready_count']}`",
        f"- Record count: `{summary['record_count']}`",
        f"- Expected yield: `{json.dumps(summary['expected_yield'], ensure_ascii=False, sort_keys=True)}`",
        f"- Suggested decisions: `{json.dumps(summary['suggested_decision_counts'], ensure_ascii=False, sort_keys=True)}`",
        f"- Release class: `{json.dumps(summary['release_class_counts'], ensure_ascii=False, sort_keys=True)}`",
        f"- Token surface: `{json.dumps(summary['token_surface_counts'], ensure_ascii=False, sort_keys=True)}`",
        "",
        "Sem apply, ingest, issue closure, lifecycle/materializer, segment-state, reindex ou produção full.",
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
                f"- Selection reason: `{record['selection_reason']}`",
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
                "**Output atual**",
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
                "**Suggested corrected_text**",
                "",
                "```text",
                str(record.get("suggested_corrected_text") or ""),
                "```",
                "",
                "Decisão humana: `approve_already_ok | corrected_text | needs_more_context | parser_later | reject`",
                "",
            ]
        )
    return "\n".join(lines) + "\n"


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_release_readiness_narrative_post580_strict_roi_human_packet"
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
    print(f"eligible_ready_count={summary['eligible_ready_count']}")
    print(f"record_count={summary['record_count']}")
    print(f"expected_yield={json.dumps(summary['expected_yield'], ensure_ascii=False, sort_keys=True)}")
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
