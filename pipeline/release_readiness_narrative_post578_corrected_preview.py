from __future__ import annotations

import argparse
import difflib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_safe_output_updates import protected_tokens
from apply_segment_state_updates import canonical_localization_text


SOURCE = "release_readiness_narrative_post578_corrected_preview_v1"
PACKET_JSONL = Path("reports/20260703_143645_067441_release_readiness_narrative_plain_light_human_packet.jsonl")

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


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only diff preview for post-578 narrative corrected_text suggestions.")
    parser.add_argument("--packet-jsonl", type=Path, default=PACKET_JSONL)
    return parser.parse_args()


def read_corrected_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with db.project_path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("suggested_decision") == "corrected_text":
                rows.append(row)
    return rows


def has_banned_surface(row: dict[str, Any]) -> bool:
    blob = "\n".join(
        str(row.get(key) or "")
        for key in ("spanish_text", "english_text", "output_text", "confirmed_text", "suggested_corrected_text", "source_key")
    )
    return any(pattern in blob for pattern in BANNED_PATTERNS) or "\n" in str(row.get("output_text") or "")


def token_integrity_ok(old: str, proposed: str) -> bool:
    if not proposed:
        return True
    return Counter(protected_tokens(old)) == Counter(protected_tokens(proposed))


def structure_integrity_ok(old: str, proposed: str) -> bool:
    if not proposed:
        return True
    if "\n" in proposed or "\r" in proposed:
        return False
    for marker in ("#T", "#X", "#P", "#N", "#EMP", "#weak", "#WEAK", "#!", "#F", "@warning_icon!"):
        if (marker in old) != (marker in proposed):
            return False
    for char in ("[", "]", "$"):
        if old.count(char) != proposed.count(char):
            return False
    return True


def classify(row: dict[str, Any], proposed: str, token_ok: bool, structure_ok: bool, canonical_change: bool, banned: bool) -> tuple[str, list[str]]:
    reasons: list[str] = []
    open_count = int(row.get("open_issue_count") or 0)
    high_count = int(row.get("high_issue_count") or 0)
    if banned:
        return "parser_later", ["banned_dynamic_or_structural_surface"]
    if not token_ok or not structure_ok:
        if not token_ok:
            reasons.append("token_integrity_failed")
        if not structure_ok:
            reasons.append("structure_integrity_failed")
        return "token_or_structure_mismatch", reasons
    if not proposed:
        if open_count or high_count:
            return "open_issue_needs_triage", ["missing_human_corrected_text", "open_or_high_issue_present"]
        return "no_output_change_learn_only", ["missing_human_corrected_text", "output_equals_confirmed"]
    if not canonical_change:
        return "no_output_change_learn_only", ["canonical_l10n_no_change"]
    if open_count or high_count:
        # Still ready only if a concrete correction exists and the surface is simple.
        return "ready_for_protected_apply", ["concrete_correction_present_with_open_issues_to_close_after_apply"]
    return "ready_for_protected_apply", []


def issue_resolution_class(row: dict[str, Any], status: str) -> str:
    if int(row.get("open_issue_count") or 0) == 0 and int(row.get("high_issue_count") or 0) == 0:
        return "unrelated_or_superseded"
    if status == "ready_for_protected_apply":
        return "resolved_by_corrected_text"
    if status == "parser_later":
        return "still_open_after_corrected_text"
    return "needs_human_context"


def build_records(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in rows:
        current = str(row.get("output_text") or "")
        proposed = str(row.get("suggested_corrected_text") or "").strip()
        token_ok = token_integrity_ok(current, proposed)
        structure_ok = structure_integrity_ok(current, proposed)
        canonical_change = bool(proposed) and canonical_localization_text(current) != canonical_localization_text(proposed)
        banned = has_banned_surface(row)
        status, reasons = classify(row, proposed, token_ok, structure_ok, canonical_change, banned)
        records.append(
            {
                "source": SOURCE,
                "record_type": "corrected_text_diff_preview",
                "segment_id": int(row["segment_id"]),
                "review_index": int(row.get("review_index") or 0),
                "relative_path": row.get("relative_path"),
                "source_key": row.get("source_key"),
                "release_class": row.get("release_class"),
                "token_surface": row.get("token_surface"),
                "packet_risk_type": row.get("packet_risk_type"),
                "source_text": row.get("spanish_text"),
                "english_text": row.get("english_text"),
                "current_output_text": current,
                "confirmed_text": row.get("confirmed_text"),
                "proposed_corrected_text": proposed,
                "token_integrity_ok": token_ok,
                "structure_integrity_ok": structure_ok,
                "canonical_l10n_changes": canonical_change,
                "open_issue_count": int(row.get("open_issue_count") or 0),
                "high_issue_count": int(row.get("high_issue_count") or 0),
                "issue_families": row.get("issue_families") or "",
                "issue_kinds": row.get("issue_kinds") or "",
                "issue_resolution_class": issue_resolution_class(row, status),
                "banned_dynamic_or_structural_surface": banned,
                "classification": status,
                "block_reasons": reasons,
                "diff_preview": (
                    list(difflib.unified_diff([current], [proposed], fromfile="current_output", tofile="proposed_corrected", lineterm=""))
                    if proposed
                    else []
                ),
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
    return records


def write_reports(records: list[dict[str, Any]], packet_jsonl: Path) -> tuple[Path, Path, Path]:
    class_counts = Counter(record["classification"] for record in records)
    ready = [record for record in records if record["classification"] == "ready_for_protected_apply"]
    no_change = [record for record in records if record["classification"] == "no_output_change_learn_only"]
    blocked = [record for record in records if record["classification"] not in {"ready_for_protected_apply", "no_output_change_learn_only"}]
    summary: dict[str, Any] = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_diff_preview",
        "input_jsonl": str(packet_jsonl),
        "record_count": len(records),
        "ready_count": len(ready),
        "no_output_change_count": len(no_change),
        "blocked_count": len(blocked),
        "ids_by_class": {
            name: [int(record["segment_id"]) for record in records if record["classification"] == name]
            for name in [
                "ready_for_protected_apply",
                "no_output_change_learn_only",
                "open_issue_needs_triage",
                "needs_more_context",
                "token_or_structure_mismatch",
                "parser_later",
            ]
        },
        "classification_counts": dict(class_counts.most_common()),
        "token_integrity_ok_count": sum(1 for record in records if record["token_integrity_ok"]),
        "structure_integrity_ok_count": sum(1 for record in records if record["structure_integrity_ok"]),
        "canonical_l10n_changes_count": sum(1 for record in records if record["canonical_l10n_changes"]),
        "issue_resolution_class_counts": dict(Counter(record["issue_resolution_class"] for record in records).most_common()),
        "block_reason_counts": dict(Counter(reason for record in blocked for reason in record["block_reasons"]).most_common()),
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
            "No protected apply is available until human corrected_text is provided for the open/high issue rows."
        ),
    }
    base = reports_dir() / f"{stamp()}_release_readiness_narrative_post578_corrected_preview"
    md_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {"markdown": str(md_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "# Narrative post-578 corrected_text diff preview",
        "",
        f"- record_count: {summary['record_count']}",
        f"- ready_count: {summary['ready_count']}",
        f"- no_output_change_count: {summary['no_output_change_count']}",
        f"- blocked_count: {summary['blocked_count']}",
        f"- classification_counts: `{json.dumps(summary['classification_counts'], ensure_ascii=False, sort_keys=True)}`",
        f"- block_reason_counts: `{json.dumps(summary['block_reason_counts'], ensure_ascii=False, sort_keys=True)}`",
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
                f"## {record['review_index']}. Segment {record['segment_id']} - {record['classification']}",
                "",
                f"- path: `{record['relative_path']}`",
                f"- key: `{record['source_key']}`",
                f"- risk: `{record['packet_risk_type']}`",
                f"- issues: open={record['open_issue_count']} high={record['high_issue_count']}",
                f"- issue_resolution_class: `{record['issue_resolution_class']}`",
                f"- block_reasons: `{json.dumps(record['block_reasons'], ensure_ascii=False)}`",
                "",
                "**Current output**",
                "",
                "```text",
                record["current_output_text"],
                "```",
                "",
                "**Proposed corrected text**",
                "",
                "```text",
                record["proposed_corrected_text"],
                "```",
                "",
            ]
        )
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path, jsonl_path, summary_path


def main() -> None:
    args = parse_args()
    rows = read_corrected_rows(args.packet_jsonl)
    records = build_records(rows)
    md_path, jsonl_path, summary_path = write_reports(records, args.packet_jsonl)
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    print(f"markdown={md_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"record_count={summary['record_count']}")
    print(f"ready_count={summary['ready_count']}")
    print(f"no_output_change_count={summary['no_output_change_count']}")
    print(f"blocked_count={summary['blocked_count']}")
    print(f"classification_counts={json.dumps(summary['classification_counts'], ensure_ascii=False, sort_keys=True)}")
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
