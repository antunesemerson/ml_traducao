from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "release_readiness_ui_tooltips_packet2_corrected_issue_triage_v1"
PREVIEW_JSONL = Path("reports/20260702_181935_011092_release_readiness_ui_tooltips_packet2_corrected_preview.jsonl")
LEDGER_RUN_ID = 76


TRIAGE_BY_SEGMENT: dict[int, dict[str, str]] = {
    143193: {
        "class": "resolved_by_corrected_text",
        "reason": "#BER todo#! is corrected to #BER qualquer#!, matching Spanish cualquier and resolving the semantic/autofix concern.",
    },
    143196: {
        "class": "resolved_by_corrected_text",
        "reason": "#BER todo#! is corrected to #BER qualquer#! and preposition is improved from em seu realm to com seu realm.",
    },
    143928: {
        "class": "resolved_by_corrected_text",
        "reason": "Preposition changes from proteger de esquemas to proteger contra esquemas, resolving the semantic fluency concern.",
    },
    158142: {
        "class": "still_open_after_corrected_text",
        "reason": "Preview target is identical to output and keeps mojibake năo; corrected target must use não before apply.",
    },
    159166: {
        "class": "still_open_after_corrected_text",
        "reason": "Preview target is identical to output and keeps mojibake năo/dę/vocę; corrected target must use não/dê/você before apply.",
    },
    160779: {
        "class": "still_open_after_corrected_text",
        "reason": "Preview target is identical to output and keeps mojibake vocę; corrected target must use você before apply.",
    },
}


FOLLOWUP_TARGETS: dict[int, str] = {
    158142: "Sua [capital|lE] não está localizada em uma [situation|lE] que use [migration|lE]",
    159166: "#TUT Embora alongar sua rota de viagem não lhe dê mais vítimas em potencial, encurtá-la pode reduzir a quantidade que você recebe#!",
    160779: "A vítima foge antes que você possa encurralá-la",
}


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only issue triage for packet2 corrected_text subbatch.")
    parser.add_argument("--preview-jsonl", type=Path, default=PREVIEW_JSONL)
    parser.add_argument("--ledger-run-id", type=int, default=LEDGER_RUN_ID)
    return parser.parse_args()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with db.project_path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def fetch_issues(conn: sqlite3.Connection, ledger_run_id: int, segment_ids: list[int]) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT *
        FROM ml_issue_ledger_items
        WHERE run_id = ?
          AND segment_id IN ({placeholders})
          AND COALESCE(status, 'open') NOT IN ('closed', 'resolved', 'dismissed')
        ORDER BY segment_id, id
        """,
        [ledger_run_id, *segment_ids],
    ).fetchall()
    return [dict(row) for row in rows]


def build_records(args: argparse.Namespace) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    preview_rows = {int(row["segment_id"]): row for row in read_jsonl(args.preview_jsonl)}
    segment_ids = sorted(preview_rows)
    if sorted(TRIAGE_BY_SEGMENT) != segment_ids:
        raise SystemExit("triage segment ids do not match preview")
    with connect_readonly() as conn:
        issues = fetch_issues(conn, args.ledger_run_id, segment_ids)
    issue_records: list[dict[str, Any]] = []
    for issue in issues:
        segment_id = int(issue["segment_id"])
        triage = TRIAGE_BY_SEGMENT[segment_id]
        preview = preview_rows[segment_id]
        issue_records.append(
            {
                "source": SOURCE,
                "record_type": "issue_triage",
                "issue_id": int(issue["id"]),
                "segment_id": segment_id,
                "relative_path": issue["relative_path"],
                "source_key": issue["source_key"],
                "issue_family": issue["issue_family"],
                "issue_kind": issue["issue_kind"],
                "issue_severity": issue["issue_severity"],
                "agent_key": issue["agent_key"],
                "triage_class": triage["class"],
                "triage_reason": triage["reason"],
                "old_output_text": preview["old_output_text"],
                "preview_target_text": preview["target_text"],
                "followup_target_text": FOLLOWUP_TARGETS.get(segment_id),
                "issue_status_now": issue["status"],
                "candidate_generation_count": 0,
                "apply_count": 0,
                "lifecycle_count": 0,
                "segment_state_count": 0,
                "reindex_count": 0,
                "production_full_count": 0,
            }
        )
    segment_records: list[dict[str, Any]] = []
    by_segment: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for record in issue_records:
        by_segment[int(record["segment_id"])].append(record)
    for segment_id in segment_ids:
        preview = preview_rows[segment_id]
        classes = Counter(record["triage_class"] for record in by_segment[segment_id])
        eligible = set(classes) == {"resolved_by_corrected_text"}
        segment_records.append(
            {
                "source": SOURCE,
                "record_type": "segment_triage_summary",
                "segment_id": segment_id,
                "relative_path": preview["relative_path"],
                "source_key": preview["source_key"],
                "issue_count": len(by_segment[segment_id]),
                "triage_class_counts": dict(classes),
                "eligible_for_protected_apply": eligible,
                "blocked_after_triage": not eligible,
                "block_reason": "" if eligible else "still_open_after_corrected_text_requires_followup_target",
                "old_output_text": preview["old_output_text"],
                "preview_target_text": preview["target_text"],
                "followup_target_text": FOLLOWUP_TARGETS.get(segment_id),
                "candidate_generation_count": 0,
                "apply_count": 0,
                "lifecycle_count": 0,
                "segment_state_count": 0,
                "reindex_count": 0,
                "production_full_count": 0,
            }
        )
    return issue_records, segment_records


def write_reports(issue_records: list[dict[str, Any]], segment_records: list[dict[str, Any]], args: argparse.Namespace) -> tuple[Path, Path, Path]:
    class_counts = Counter(record["triage_class"] for record in issue_records)
    eligible_segments = [record["segment_id"] for record in segment_records if record["eligible_for_protected_apply"]]
    blocked_segments = [record["segment_id"] for record in segment_records if record["blocked_after_triage"]]
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_issue_triage",
        "preview_jsonl": str(args.preview_jsonl),
        "ledger_run_id": args.ledger_run_id,
        "segment_count": len(segment_records),
        "issue_count": len(issue_records),
        "issue_class_counts": dict(class_counts),
        "segment_summary": {
            str(record["segment_id"]): {
                "issue_count": record["issue_count"],
                "triage_class_counts": record["triage_class_counts"],
                "eligible_for_protected_apply": record["eligible_for_protected_apply"],
                "blocked_after_triage": record["blocked_after_triage"],
                "block_reason": record["block_reason"],
                "followup_target_text": record["followup_target_text"],
            }
            for record in segment_records
        },
        "eligible_for_protected_apply_segment_ids": eligible_segments,
        "blocked_segment_ids": blocked_segments,
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "single_operational_recommendation": (
            "Apply only the 3 eligible segments now, or first fix the preview targets for the 3 mojibake segments and rerun diff preview."
        ),
    }
    base = reports_dir() / f"{stamp()}_release_readiness_ui_tooltips_packet2_corrected_issue_triage"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in [*segment_records, *issue_records]:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "UI/tooltips packet2 corrected_text issue triage",
        f"segment_count={summary['segment_count']}",
        f"issue_count={summary['issue_count']}",
        f"issue_class_counts={json.dumps(summary['issue_class_counts'], ensure_ascii=False, sort_keys=True)}",
        f"eligible_for_protected_apply_segment_ids={eligible_segments}",
        f"blocked_segment_ids={blocked_segments}",
        "candidate_generation_count=0",
        "apply_count=0",
        "lifecycle_count=0",
        "segment_state_count=0",
        "reindex_count=0",
        "production_full_count=0",
        "",
        "Segment summaries:",
    ]
    for record in segment_records:
        lines.append(
            f"- {record['segment_id']}: eligible={record['eligible_for_protected_apply']} "
            f"classes={record['triage_class_counts']} block={record['block_reason']}"
        )
        if record["followup_target_text"]:
            lines.append(f"  followup_target={record['followup_target_text']}")
    lines.append("")
    lines.append(f"recommendation={summary['single_operational_recommendation']}")
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    args = parse_args()
    issue_records, segment_records = build_records(args)
    txt_path, jsonl_path, summary_path = write_reports(issue_records, segment_records, args)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"segment_count={len(segment_records)}")
    print(f"issue_count={len(issue_records)}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
