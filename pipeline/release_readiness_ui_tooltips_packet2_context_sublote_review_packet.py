from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "release_readiness_ui_tooltips_packet2_context_sublote_review_packet_v1"
DIAGNOSTIC_JSONL = Path("reports/20260702_211100_399412_release_readiness_ui_tooltips_packet2_needs_context_diagnostic.jsonl")
TARGET_SEGMENT_IDS = [
    155710,
    32067,
    70488,
    73726,
    49517,
    53325,
    81009,
    64677,
    70505,
    62979,
    29838,
    29839,
    29840,
    29841,
    27635,
]


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_rows() -> list[dict[str, Any]]:
    by_id: dict[int, dict[str, Any]] = {}
    with db.project_path(DIAGNOSTIC_JSONL).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            row["_diagnostic_line_number"] = line_number
            by_id[int(row["segment_id"])] = row
    missing = sorted(set(TARGET_SEGMENT_IDS) - set(by_id))
    if missing:
        raise SystemExit(f"missing diagnostic rows: {missing}")
    return [by_id[segment_id] for segment_id in TARGET_SEGMENT_IDS]


def suggested_decision(row: dict[str, Any]) -> str:
    if row["operational_family"] == "human_simple_context_review":
        return "approve_already_ok"
    if row["operational_family"] == "spanish_residue_visible":
        return "corrected_text"
    return row.get("recommendation") or "needs_more_context"


def build_records(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for index, row in enumerate(rows, 1):
        records.append(
            {
                "source": SOURCE,
                "record_type": "human_review_packet_item",
                "review_order": index,
                "segment_id": int(row["segment_id"]),
                "relative_path": row.get("relative_path"),
                "source_key": row.get("source_key"),
                "token_surface": row.get("token_surface"),
                "operational_family": row.get("operational_family"),
                "why_needs_context": row.get("why_needs_context"),
                "source_text": row.get("source_text"),
                "english_text": row.get("english_text"),
                "output_text": row.get("output_text"),
                "confirmed_text": row.get("confirmed_text"),
                "issue_family": row.get("issue_family"),
                "issue_kind": row.get("issue_kind"),
                "open_issue_count": int(row.get("open_issue_count") or 0),
                "high_issue_count": int(row.get("high_issue_count") or 0),
                "suggested_decision": suggested_decision(row),
                "allowed_decisions": [
                    "approve_already_ok",
                    "corrected_text",
                    "needs_more_context",
                    "parser_later",
                    "hold",
                    "reject_candidate",
                ],
                "candidate_generation_count": 0,
                "apply_count": 0,
                "lifecycle_count": 0,
                "segment_state_count": 0,
                "reindex_count": 0,
                "production_full_count": 0,
            }
        )
    return records


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_release_readiness_ui_tooltips_packet2_context_sublote_review_packet"
    md_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {"markdown": str(md_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "# UI/tooltips packet2 context sublote review",
        "",
        "Decisões permitidas: `approve_already_ok`, `corrected_text`, `needs_more_context`, `parser_later`, `hold`, `reject_candidate`.",
        "",
    ]
    for record in records:
        lines.extend(
            [
                f"## {record['review_order']}. segment_id={record['segment_id']}",
                f"- Arquivo: `{record['relative_path']}`",
                f"- Chave: `{record['source_key']}`",
                f"- Família: `{record['operational_family']}`",
                f"- Token surface: `{record['token_surface']}`",
                f"- Issues abertas/high: `{record['open_issue_count']}` / `{record['high_issue_count']}`",
                f"- Por que precisa contexto: {record['why_needs_context']}",
                f"- Sugestão: `{record['suggested_decision']}`",
                "",
                "Source:",
                "```text",
                str(record.get("source_text") or ""),
                "```",
                "Output atual:",
                "```text",
                str(record.get("output_text") or ""),
                "```",
                "Confirmed atual:",
                "```text",
                str(record.get("confirmed_text") or ""),
                "```",
                "Decisão humana:",
                "",
                "",
            ]
        )
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path, jsonl_path, summary_path


def main() -> None:
    rows = read_rows()
    records = build_records(rows)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_human_review_packet",
        "diagnostic_jsonl": str(DIAGNOSTIC_JSONL),
        "record_count": len(records),
        "segment_ids": [int(record["segment_id"]) for record in records],
        "family_counts": dict(Counter(record["operational_family"] for record in records).most_common()),
        "suggested_decision_counts": dict(Counter(record["suggested_decision"] for record in records).most_common()),
        "token_surface_counts": dict(Counter(record["token_surface"] for record in records).most_common()),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "single_operational_recommendation": (
            "Review this packet manually. After decisions are provided, ingest confirmed signals; apply protected only for corrected_text items."
        ),
    }
    md, jsonl, summary_path = write_reports(records, summary)
    print(f"markdown={md}")
    print(f"jsonl={jsonl}")
    print(f"summary={summary_path}")
    print(f"record_count={summary['record_count']}")
    print(f"family_counts={json.dumps(summary['family_counts'], ensure_ascii=False, sort_keys=True)}")
    print(f"suggested_decision_counts={json.dumps(summary['suggested_decision_counts'], ensure_ascii=False, sort_keys=True)}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
