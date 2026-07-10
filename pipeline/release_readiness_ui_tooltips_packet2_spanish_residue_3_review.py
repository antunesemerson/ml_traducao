from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_safe_output_updates import protected_tokens


SOURCE = "release_readiness_ui_tooltips_packet2_spanish_residue_3_review_v1"
SEGMENT_STATE_RUN_ID = 564
LEDGER_RUN_ID = 76
TARGET_IDS = [27645, 57841, 77087]

DECISIONS = {
    27645: {
        "suggested_decision": "corrected_text",
        "corrected_text": "#D     Força atribuída real: $NUM$#!",
        "reason": "Output is still Spanish; this is a short debug tooltip with one variable and debug marker.",
    },
    57841: {
        "suggested_decision": "approve_already_ok",
        "corrected_text": "",
        "reason": "El Cid is a proper-name/title surface in the localized string; no actionable Spanish residue in the sentence.",
    },
    77087: {
        "suggested_decision": "approve_already_ok",
        "corrected_text": "",
        "reason": "Visible prose is PT-BR; [bloc|El] and [hook|El] are CK3 token pipes and should be preserved.",
    },
}


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def issue_rows(conn: sqlite3.Connection, segment_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT issue_family, issue_kind, issue_severity, status, validation_status
        FROM ml_issue_ledger_items
        WHERE run_id = ?
          AND segment_id = ?
          AND COALESCE(status, 'open') NOT IN ('closed', 'resolved', 'dismissed')
        ORDER BY issue_family, issue_kind
        """,
        (LEDGER_RUN_ID, segment_id),
    ).fetchall()
    return [dict(row) for row in rows]


def build() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    conn = db.connect(db.load_settings())
    conn.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in TARGET_IDS)
    rows = conn.execute(
        f"""
        SELECT
          state.segment_id,
          state.relative_path,
          state.source_key,
          state.final_state,
          state.state_group,
          state.review_state,
          state.confirmation_level,
          state.confirmation_label,
          state.locked,
          state.confirmed_matches_output,
          state.needs_output_apply,
          src.spanish_text,
          src.english_text,
          output.portuguese_text AS output_text,
          conf.confirmed_text
        FROM segment_state_items state
        JOIN source_segments src ON src.id = state.segment_id
        LEFT JOIN output_segments output ON output.segment_id = state.segment_id
        LEFT JOIN segment_confirmations conf ON conf.segment_id = state.segment_id
        WHERE state.run_id = ?
          AND state.segment_id IN ({placeholders})
        ORDER BY state.segment_id
        """,
        [SEGMENT_STATE_RUN_ID, *TARGET_IDS],
    ).fetchall()
    records: list[dict[str, Any]] = []
    for row in rows:
        row_dict = dict(row)
        segment_id = int(row_dict["segment_id"])
        issues = issue_rows(conn, segment_id)
        decision = DECISIONS[segment_id]
        records.append(
            {
                "source": SOURCE,
                "record_type": "spanish_residue_3_review_item",
                **row_dict,
                "open_issue_count": len(issues),
                "high_issue_count": sum(
                    1 for issue in issues if str(issue.get("issue_severity") or "").lower() in {"high", "critical", "error"}
                ),
                "issue_rows": issues,
                "protected_tokens_output": protected_tokens(row_dict.get("output_text") or ""),
                "suggested_decision": decision["suggested_decision"],
                "corrected_text": decision["corrected_text"],
                "decision_reason": decision["reason"],
                "candidate_generation_count": 0,
                "apply_count": 0,
                "lifecycle_count": 0,
                "segment_state_count": 0,
                "reindex_count": 0,
                "production_full_count": 0,
            }
        )
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_human_review_packet",
        "segment_state_run_id": SEGMENT_STATE_RUN_ID,
        "ledger_run_id": LEDGER_RUN_ID,
        "record_count": len(records),
        "decision_counts": dict(Counter(record["suggested_decision"] for record in records).most_common()),
        "open_issue_count": sum(int(record["open_issue_count"]) for record in records),
        "high_issue_count": sum(int(record["high_issue_count"]) for record in records),
        "corrected_text_ids": [record["segment_id"] for record in records if record["suggested_decision"] == "corrected_text"],
        "approve_already_ok_ids": [record["segment_id"] for record in records if record["suggested_decision"] == "approve_already_ok"],
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "single_operational_recommendation": (
            "If human approves: apply protected correction only for 27645, ingest/confirm all 3, close resolved issues, then lifecycle bridge only after segment-state shows output==confirmed and no open issues."
        ),
    }
    return records, summary


def write(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_release_readiness_ui_tooltips_packet2_spanish_residue_3_review"
    txt_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {"markdown": str(txt_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "# UI/tooltips packet2 - spanish residue 3 review",
        "",
        f"- record_count: {summary['record_count']}",
        f"- decision_counts: {json.dumps(summary['decision_counts'], ensure_ascii=False, sort_keys=True)}",
        f"- open_issue_count: {summary['open_issue_count']}",
        f"- high_issue_count: {summary['high_issue_count']}",
        "- candidate_generation_count: 0",
        "- apply_count: 0",
        "- lifecycle_count: 0",
        "- segment_state_count: 0",
        "- reindex_count: 0",
        "- production_full_count: 0",
        "",
    ]
    for record in records:
        lines.extend(
            [
                f"## {record['segment_id']} - {record['source_key']}",
                "",
                f"- path: `{record['relative_path']}`",
                f"- state: `{record['final_state']}` / `{record['state_group']}`",
                f"- issues: open={record['open_issue_count']} high={record['high_issue_count']}",
                f"- suggested_decision: `{record['suggested_decision']}`",
                f"- reason: {record['decision_reason']}",
                "",
                "**Source ES**",
                "",
                "```text",
                str(record.get("spanish_text") or ""),
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
                str(record.get("output_text") or ""),
                "```",
                "",
                "**Confirmed text**",
                "",
                "```text",
                str(record.get("confirmed_text") or ""),
                "```",
                "",
            ]
        )
        if record["corrected_text"]:
            lines.extend(["**Proposed corrected_text**", "", "```text", record["corrected_text"], "```", ""])
    lines.extend(["## Recommendation", "", summary["single_operational_recommendation"], ""])
    txt_path.write_text("\n".join(lines), encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    records, summary = build()
    txt_path, jsonl_path, summary_path = write(records, summary)
    print(f"markdown={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"record_count={summary['record_count']}")
    print(f"decision_counts={json.dumps(summary['decision_counts'], ensure_ascii=False, sort_keys=True)}")
    print(f"open_issue_count={summary['open_issue_count']}")
    print(f"high_issue_count={summary['high_issue_count']}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
