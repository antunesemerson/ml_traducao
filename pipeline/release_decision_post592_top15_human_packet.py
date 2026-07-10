from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "release_decision_post592_top15_human_packet_v1"
DEFAULT_INPUT_JSONL = Path("reports/20260703_234945_493684_release_decision_post592_report_readonly.jsonl")
TARGET_IDS = (
    79601,
    112693,
    112699,
    104908,
    121866,
    121868,
    124242,
    127935,
    132509,
    132516,
    132520,
    134297,
    30753,
    31096,
    31387,
)


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Editable human packet for top-15 post-592 corrected_text blockers.")
    parser.add_argument("--input-jsonl", type=Path, default=DEFAULT_INPUT_JSONL)
    return parser.parse_args()


def read_rows(path: Path) -> dict[int, dict[str, Any]]:
    rows: dict[int, dict[str, Any]] = {}
    with db.project_path(path).open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            rows[int(row["segment_id"])] = row
    return rows


def source_context(conn, segment_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT relative_path, source_line_number, source_key, spanish_text, english_text, spanish_raw_line, english_raw_line
        FROM source_segments
        WHERE id = ?
        """,
        (segment_id,),
    ).fetchone()
    return dict(row) if row else {}


def issue_rows(conn, segment_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, issue_type, severity, message, details_json, created_at
        FROM issues
        WHERE segment_id = ?
        ORDER BY
          CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 WHEN 'low' THEN 2 ELSE 3 END,
          issue_type,
          id
        """,
        (segment_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def risk_summary(row: dict[str, Any]) -> str:
    risks: list[str] = []
    if row.get("token_surface") != "plain_text":
        risks.append(f"token_surface={row.get('token_surface')}")
    if row.get("has_context_or_token_signal"):
        risks.append("token/context signal present")
    if row.get("has_spanish_residue_signal"):
        risks.append("spanish residue signal")
    if int(row.get("high_issue_count") or 0) > 0:
        risks.append(f"high_issue_count={row.get('high_issue_count')}")
    return "; ".join(risks) if risks else "plain text, no structural risk signal in report"


def main() -> None:
    args = parse_args()
    rows_by_id = read_rows(args.input_jsonl)
    missing = [segment_id for segment_id in TARGET_IDS if segment_id not in rows_by_id]
    if missing:
        raise SystemExit(f"missing target ids in input jsonl: {missing}")
    conn = db.connect(db.load_settings())
    records: list[dict[str, Any]] = []
    for index, segment_id in enumerate(TARGET_IDS, 1):
        row = dict(rows_by_id[segment_id])
        source = source_context(conn, segment_id)
        issues = issue_rows(conn, segment_id)
        high_issues = [issue for issue in issues if issue.get("severity") == "high"]
        record = {
            "schema_version": 1,
            "source": SOURCE,
            "packet_index": index,
            "segment_id": segment_id,
            "relative_path": row.get("relative_path") or source.get("relative_path"),
            "source_line_number": source.get("source_line_number"),
            "source_key": row.get("source_key") or source.get("source_key"),
            "spanish_text": source.get("spanish_text"),
            "english_text": source.get("english_text"),
            "output_text": row.get("output_text"),
            "current_target_or_confirmed_text": row.get("confirmed_text"),
            "token_surface": row.get("token_surface"),
            "fixability": row.get("fixability"),
            "visibility": row.get("visibility"),
            "open_issue_count": row.get("open_issue_count"),
            "high_issue_count": row.get("high_issue_count"),
            "issue_families": row.get("issue_families"),
            "issue_kinds": row.get("issue_kinds"),
            "issue_ids": [issue["id"] for issue in issues],
            "high_issue_ids": [issue["id"] for issue in high_issues],
            "issue_preview": [
                {
                    "id": issue["id"],
                    "issue_type": issue["issue_type"],
                    "severity": issue["severity"],
                    "message": issue["message"],
                }
                for issue in issues[:8]
            ],
            "correction_reason": row.get("issue_kinds") or row.get("issue_families") or "high issue/manual corrected_text review",
            "token_structure_risk": risk_summary(row),
            "human_decision": "",
            "corrected_text": "",
            "notes": "",
            "allowed_human_decisions": [
                "corrected_text",
                "approve_already_ok",
                "needs_more_context",
                "parser_later",
                "reject",
            ],
        }
        records.append(record)
    counts = {
        "token_surface": dict(Counter(str(r["token_surface"]) for r in records).most_common()),
        "fixability": dict(Counter(str(r["fixability"]) for r in records).most_common()),
        "relative_path": dict(Counter(str(r["relative_path"]) for r in records).most_common()),
    }
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "human_editable_packet_read_only",
        "input_jsonl": str(args.input_jsonl),
        "record_count": len(records),
        "target_segment_ids": list(TARGET_IDS),
        "counts": counts,
        "required_fields": ["human_decision", "corrected_text when human_decision=corrected_text"],
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
        "single_operational_recommendation": "Fill the Markdown human_decision/corrected_text fields, then rerun a read-only extraction/diff preview.",
    }
    base = reports_dir() / f"{stamp()}_release_decision_post592_top15_human_packet"
    md_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    md_lines = [
        "# Release Decision Post-592 Top 15 Human Packet",
        "",
        "Preencha `Human decision` e, se escolher `corrected_text`, preencha `Corrected text` com o texto final exato.",
        "Decisões válidas: `corrected_text`, `approve_already_ok`, `needs_more_context`, `parser_later`, `reject`.",
        "",
    ]
    for record in records:
        md_lines.extend(
            [
                f"## {record['packet_index']}. Segment {record['segment_id']}",
                f"- Source key: `{record['source_key']}`",
                f"- File/line: `{record['relative_path']}:{record['source_line_number']}`",
                f"- Token surface: `{record['token_surface']}`",
                f"- Fixability: `{record['fixability']}`",
                f"- Open/high issues: `{record['open_issue_count']}` / `{record['high_issue_count']}`",
                f"- Issue families: `{record['issue_families']}`",
                f"- Issue kinds: `{record['issue_kinds']}`",
                f"- Correction reason: {record['correction_reason']}",
                f"- Token/structure risk: {record['token_structure_risk']}",
                "",
                "**Spanish/source**",
                "",
                "```text",
                str(record.get("spanish_text") or ""),
                "```",
                "",
                "**Current output**",
                "",
                "```text",
                str(record.get("output_text") or ""),
                "```",
                "",
                "**Current target/confirmed**",
                "",
                "```text",
                str(record.get("current_target_or_confirmed_text") or ""),
                "```",
                "",
                "**Issue preview**",
            ]
        )
        for issue in record["issue_preview"]:
            md_lines.append(f"- `{issue['id']}` `{issue['severity']}` `{issue['issue_type']}`: {issue['message']}")
        md_lines.extend(
            [
                "",
                "Human decision:",
                "",
                "Corrected text:",
                "",
                "Notes:",
                "",
            ]
        )
    summary["output_files"] = {"markdown": str(md_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    md_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"markdown={md_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"record_count={summary['record_count']}")
    print(f"counts={json.dumps(counts, ensure_ascii=False, sort_keys=True)}")
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
