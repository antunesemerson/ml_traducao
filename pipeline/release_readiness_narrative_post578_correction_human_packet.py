from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "release_readiness_narrative_post578_correction_human_packet_v1"
PREVIEW_JSONL = Path("reports/20260703_143925_183365_release_readiness_narrative_post578_corrected_preview.jsonl")
LEDGER_RUN_ID = 77
SEGMENT_IDS = [
    48500,
    54856,
    54888,
    67282,
    67319,
    100383,
    104983,
    105133,
    105383,
    112620,
    121588,
    121728,
    121866,
    121868,
    124242,
    126472,
    132509,
    132516,
]


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Editable human correction packet for post-578 narrative blocked rows.")
    parser.add_argument("--preview-jsonl", type=Path, default=PREVIEW_JSONL)
    parser.add_argument("--ledger-run-id", type=int, default=LEDGER_RUN_ID)
    return parser.parse_args()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with db.project_path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def fetch_open_issues(ledger_run_id: int) -> dict[int, list[dict[str, Any]]]:
    placeholders = ",".join("?" for _ in SEGMENT_IDS)
    with connect_readonly() as conn:
        rows = conn.execute(
            f"""
            SELECT
              id,
              segment_id,
              issue_family,
              issue_kind,
              issue_severity,
              status,
              evidence_text,
              evidence_json,
              proposed_repair_text,
              token_status,
              validation_status
            FROM ml_issue_ledger_items
            WHERE run_id=?
              AND segment_id IN ({placeholders})
              AND COALESCE(status, 'open') NOT IN ('closed', 'resolved', 'dismissed')
            ORDER BY segment_id, issue_family, issue_kind, id
            """,
            [ledger_run_id, *SEGMENT_IDS],
        ).fetchall()
    grouped: dict[int, list[dict[str, Any]]] = {segment_id: [] for segment_id in SEGMENT_IDS}
    for row in rows:
        item = dict(row)
        grouped[int(item["segment_id"])].append(item)
    return grouped


def compact_issue(issue: dict[str, Any]) -> dict[str, Any]:
    return {
        "issue_id": issue.get("id"),
        "issue_family": issue.get("issue_family"),
        "issue_kind": issue.get("issue_kind"),
        "issue_severity": issue.get("issue_severity"),
        "status": issue.get("status"),
        "evidence_text": issue.get("evidence_text"),
        "evidence_json": issue.get("evidence_json"),
        "proposed_repair_text": issue.get("proposed_repair_text"),
        "token_status": issue.get("token_status"),
        "validation_status": issue.get("validation_status"),
        "would_be_resolved_by_corrected_text": True,
    }


def build_records(args: argparse.Namespace) -> list[dict[str, Any]]:
    preview_by_id = {int(row["segment_id"]): row for row in read_jsonl(args.preview_jsonl)}
    missing = sorted(set(SEGMENT_IDS) - set(preview_by_id))
    if missing:
        raise SystemExit(f"missing preview rows for segment ids: {missing}")
    issues_by_id = fetch_open_issues(args.ledger_run_id)
    records: list[dict[str, Any]] = []
    for index, segment_id in enumerate(SEGMENT_IDS, 1):
        preview = preview_by_id[segment_id]
        issues = [compact_issue(issue) for issue in issues_by_id.get(segment_id, [])]
        record = {
            "source": SOURCE,
            "record_type": "human_correction_review_item",
            "review_index": index,
            "segment_id": segment_id,
            "relative_path": preview.get("relative_path"),
            "source_key": preview.get("source_key"),
            "release_class": preview.get("release_class"),
            "token_surface": preview.get("token_surface"),
            "packet_risk_type": preview.get("packet_risk_type"),
            "source_text": preview.get("source_text"),
            "english_text": preview.get("english_text"),
            "current_output_text": preview.get("current_output_text"),
            "confirmed_text": preview.get("confirmed_text"),
            "open_issue_count": int(preview.get("open_issue_count") or 0),
            "high_issue_count": int(preview.get("high_issue_count") or 0),
            "issue_families": preview.get("issue_families") or "",
            "issue_kinds": preview.get("issue_kinds") or "",
            "open_issues": issues,
            "human_decision": "",
            "human_decision_options": [
                "corrected_text",
                "approve_already_ok",
                "needs_more_context",
                "parser_later",
                "reject",
            ],
            "corrected_text_required_if_decision_is_corrected_text": True,
            "corrected_text": "",
            "human_notes": "",
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
    return records


def write_reports(records: list[dict[str, Any]], args: argparse.Namespace) -> tuple[Path, Path, Path]:
    issue_family_counts = Counter(
        issue.get("issue_family") or "none"
        for record in records
        for issue in record.get("open_issues", [])
    )
    issue_kind_counts = Counter(
        issue.get("issue_kind") or "none"
        for record in records
        for issue in record.get("open_issues", [])
    )
    summary: dict[str, Any] = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_human_correction_packet",
        "preview_jsonl": str(args.preview_jsonl),
        "ledger_run_id": args.ledger_run_id,
        "record_count": len(records),
        "segment_ids": [int(record["segment_id"]) for record in records],
        "total_open_issue_rows": sum(len(record.get("open_issues", [])) for record in records),
        "high_issue_segment_count": sum(1 for record in records if int(record.get("high_issue_count") or 0) > 0),
        "issue_family_counts": dict(issue_family_counts.most_common()),
        "issue_kind_counts": dict(issue_kind_counts.most_common()),
        "human_decision_options": [
            "corrected_text",
            "approve_already_ok",
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
        "source_changed": False,
        "output_changed": False,
        "single_operational_recommendation": (
            "Fill human_decision for each row. If human_decision=corrected_text, corrected_text must contain the exact final PT-BR text before any diff preview/apply cycle."
        ),
    }
    base = reports_dir() / f"{stamp()}_release_readiness_narrative_post578_correction_human_packet"
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


def issue_lines(record: dict[str, Any]) -> list[str]:
    issues = record.get("open_issues", [])
    if not issues:
        return ["- No open issues found in current ledger."]
    lines: list[str] = []
    for issue in issues[:12]:
        lines.append(
            f"- `{issue.get('issue_family')}` / `{issue.get('issue_kind')}` / `{issue.get('issue_severity')}`"
        )
        evidence = str(issue.get("evidence_text") or issue.get("proposed_repair_text") or "").strip()
        if evidence:
            lines.append(f"  Evidence: `{evidence[:260]}`")
    if len(issues) > 12:
        lines.append(f"- ... {len(issues) - 12} additional issue rows omitted in Markdown; see JSONL.")
    return lines


def markdown(records: list[dict[str, Any]], summary: dict[str, Any]) -> str:
    lines = [
        "# Narrative post-578 correction human packet",
        "",
        f"- record_count: {summary['record_count']}",
        f"- total_open_issue_rows: {summary['total_open_issue_rows']}",
        f"- high_issue_segment_count: {summary['high_issue_segment_count']}",
        "- Mode: read-only human correction review",
        "- No candidate generation, apply, learning ingest, issue closure, lifecycle, segment-state, reindex or production full",
        "",
        "Decision options: `corrected_text`, `approve_already_ok`, `needs_more_context`, `parser_later`, `reject`.",
        "If decision is `corrected_text`, fill the exact final PT-BR text in the fenced block.",
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
                f"- Risk: `{record['packet_risk_type']}`",
                f"- Open issues: `{record['open_issue_count']}`",
                f"- High issues: `{record['high_issue_count']}`",
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
                "**Current output**",
                "",
                "```text",
                str(record.get("current_output_text") or ""),
                "```",
                "",
                "**Current confirmed text**",
                "",
                "```text",
                str(record.get("confirmed_text") or ""),
                "```",
                "",
                "**Open issues that corrected_text should resolve**",
                "",
                *issue_lines(record),
                "",
                "Human decision: `corrected_text | approve_already_ok | needs_more_context | parser_later | reject`",
                "",
                "Corrected text, required only if decision is `corrected_text`:",
                "",
                "```text",
                "",
                "```",
                "",
                "Human notes:",
                "",
                "```text",
                "",
                "```",
                "",
            ]
        )
    return "\n".join(lines)


def main() -> None:
    args = parse_args()
    records = build_records(args)
    md_path, jsonl_path, summary_path = write_reports(records, args)
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    print(f"markdown={md_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"record_count={summary['record_count']}")
    print(f"total_open_issue_rows={summary['total_open_issue_rows']}")
    print(f"high_issue_segment_count={summary['high_issue_segment_count']}")
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
