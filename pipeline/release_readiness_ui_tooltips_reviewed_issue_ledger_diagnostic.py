from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "release_readiness_ui_tooltips_reviewed_issue_ledger_diagnostic_v1"
RUN_ID = 547
BATCH_JSONLS = [
    Path("reports/20260702_143725_050904_release_readiness_ui_tooltips_batch1_protected_apply_apply.jsonl"),
    Path("reports/20260702_145900_387262_release_readiness_ui_tooltips_batch2_protected_apply_apply.jsonl"),
    Path("reports/20260702_155807_716233_release_readiness_ui_tooltips_batch3_protected_apply_apply.jsonl"),
]


SPANISH_WORDS = {
    " años",
    " año",
    " puedes ",
    " puede ",
    " aun ",
    " aún ",
    " de otro modo",
    " comenzar",
    " comenzará",
    " contra los",
    " descontentos",
    " fuerza",
    " asignada",
    " real:",
    " nómadas",
    " nómada",
    " encarcelan",
    " ejecuten",
    " familia poderosa",
    " posesión",
    " pastora",
    " se reduce",
    " un poco",
    " terminará",
    " tu ",
    " tus ",
}


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only issue-ledger diagnostic for reviewed UI/tooltips release batches.")
    parser.add_argument("--run-id", type=int, default=RUN_ID)
    parser.add_argument("--issue-ledger-run-id", type=int)
    parser.add_argument("--batch-jsonl", action="append", type=Path)
    return parser.parse_args()


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


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


def fetch_state_rows(conn: sqlite3.Connection, run_id: int, segment_ids: list[int]) -> dict[int, dict[str, Any]]:
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT
          state.segment_id,
          state.final_state,
          state.state_group,
          state.review_state,
          state.apply_state,
          state.confirmation_level,
          state.confirmation_label,
          state.locked,
          state.confirmed_matches_output,
          state.needs_output_apply,
          state.is_closed,
          output.portuguese_text AS output_text,
          conf.confirmed_text,
          conf.confirmation_source
        FROM segment_state_items state
        LEFT JOIN output_segments output ON output.segment_id = state.segment_id
        LEFT JOIN segment_confirmations conf ON conf.segment_id = state.segment_id
        WHERE state.run_id = ?
          AND state.segment_id IN ({placeholders})
        """,
        [run_id, *segment_ids],
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def latest_issue_ledger_run_id(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT MAX(id) AS id FROM ml_issue_ledger_runs WHERE finished_at IS NOT NULL"
    ).fetchone()
    if row is None or row["id"] is None:
        raise SystemExit("missing complete ml_issue_ledger_runs")
    return int(row["id"])


def fetch_open_issues(conn: sqlite3.Connection, segment_ids: list[int], ledger_run_id: int) -> dict[int, list[dict[str, Any]]]:
    output: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for offset in range(0, len(segment_ids), 800):
        chunk = segment_ids[offset : offset + 800]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""
            SELECT *
            FROM ml_issue_ledger_items
            WHERE run_id = ?
              AND segment_id IN ({placeholders})
              AND COALESCE(status, 'open') NOT IN ('closed', 'resolved', 'dismissed')
            ORDER BY segment_id, issue_severity DESC, issue_family, issue_kind
            """,
            [ledger_run_id, *chunk],
        ).fetchall()
        for row in rows:
            output[int(row["segment_id"])].append(dict(row))
    return output


def contains_spanish_residue(text: str) -> bool:
    lowered = f" {text.lower()} "
    if any(word in lowered for word in SPANISH_WORDS):
        return True
    if re.search(r"\b(el|los|las|del|aun|años|puedes|puede|debes|tienes|consigue|comienza)\b", lowered):
        return True
    return False


def issue_reassessment(issue: dict[str, Any], row: dict[str, Any], state: dict[str, Any] | None) -> str:
    family = str(issue.get("issue_family") or "")
    kind = str(issue.get("issue_kind") or "")
    severity = str(issue.get("issue_severity") or "").lower()
    decision = str(row.get("human_decision") or "")
    final_text = str((state or {}).get("output_text") or row.get("target_text") or "")
    confirmed_matches = int((state or {}).get("confirmed_matches_output") or 0) == 1
    needs_output_apply = int((state or {}).get("needs_output_apply") or 0) == 1

    if decision == "hold_parser_later":
        return "issue_still_valid_hold_parser_later"
    if needs_output_apply or not confirmed_matches:
        return "issue_still_valid_state_guard"
    if "spanish_residual" in family or "spanish_residue" in kind:
        return "likely_resolved_by_human_review" if not contains_spanish_residue(final_text) else "issue_still_valid_spanish_residue_visible"
    if family == "high_issue_auditor":
        return "needs_human_issue_triage_after_correction"
    if family in {"semantic_review_router", "short_label_style_microagent", "autofix_unknown_microagent"}:
        if decision in {"corrected_text", "approve_already_ok"}:
            return "likely_resolved_by_human_review"
    if "dynamic_ck3_expression" in family:
        return "needs_parser_or_token_policy_review" if severity in {"high", "critical"} else "review_before_issue_closure"
    return "review_before_issue_closure"


def recommended_action(reassessment: str) -> str:
    if reassessment == "likely_resolved_by_human_review":
        return "candidate_for_issue_closure_dry_run"
    if reassessment == "needs_human_issue_triage_after_correction":
        return "human_issue_triage_before_closure"
    if reassessment.startswith("issue_still_valid"):
        return "keep_issue_open"
    if reassessment == "needs_parser_or_token_policy_review":
        return "architecture_or_parser_review"
    return "manual_issue_review"


def build(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    batch_paths = args.batch_jsonl or BATCH_JSONLS
    decisions: dict[int, dict[str, Any]] = {}
    for path in batch_paths:
        for row in read_jsonl(path):
            segment_id = int(row["segment_id"])
            decisions[segment_id] = {**row, "batch_jsonl": str(path)}
    segment_ids = sorted(decisions)
    if len(segment_ids) != 36:
        raise SystemExit(f"expected 36 reviewed segment ids, got {len(segment_ids)}")

    with connect_readonly() as conn:
        ledger_run_id = args.issue_ledger_run_id or latest_issue_ledger_run_id(conn)
        state_rows = fetch_state_rows(conn, args.run_id, segment_ids)
        issues = fetch_open_issues(conn, segment_ids, ledger_run_id)

    records: list[dict[str, Any]] = []
    samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for segment_id in segment_ids:
        decision = decisions[segment_id]
        state = state_rows.get(segment_id)
        open_issues = issues.get(segment_id, [])
        if not open_issues:
            record = {
                "source": SOURCE,
                "record_type": "reviewed_segment_no_open_issue",
                "segment_id": segment_id,
                "relative_path": decision.get("relative_path"),
                "source_key": decision.get("source_key"),
                "human_decision": decision.get("human_decision"),
                "final_state": (state or {}).get("final_state"),
                "recommended_action": "no_issue_action_needed",
                "candidate_generation_count": 0,
                "apply_count": 0,
                "lifecycle_count": 0,
                "segment_state_count": 0,
                "reindex_count": 0,
                "production_full_count": 0,
            }
            records.append(record)
            continue
        for issue in open_issues:
            reassessment = issue_reassessment(issue, decision, state)
            record = {
                "source": SOURCE,
                "record_type": "reviewed_segment_open_issue_reassessment",
                "segment_id": segment_id,
                "relative_path": decision.get("relative_path"),
                "source_key": decision.get("source_key"),
                "human_decision": decision.get("human_decision"),
                "requires_output_write": bool(decision.get("requires_output_write")),
                "confirmation_label_after": decision.get("confirmation_label_after"),
                "final_state": (state or {}).get("final_state"),
                "state_group": (state or {}).get("state_group"),
                "confirmed_matches_output": int((state or {}).get("confirmed_matches_output") or 0),
                "needs_output_apply": int((state or {}).get("needs_output_apply") or 0),
                "issue_id": int(issue["id"]),
                "issue_run_id": issue.get("run_id"),
                "issue_family": issue.get("issue_family"),
                "issue_kind": issue.get("issue_kind"),
                "issue_severity": issue.get("issue_severity"),
                "issue_status": issue.get("status"),
                "agent_key": issue.get("agent_key"),
                "route_status": issue.get("route_status"),
                "token_status": issue.get("token_status"),
                "evidence_text": issue.get("evidence_text"),
                "reassessment": reassessment,
                "recommended_action": recommended_action(reassessment),
                "output_text": (state or {}).get("output_text"),
                "confirmed_text": (state or {}).get("confirmed_text"),
                "candidate_generation_count": 0,
                "apply_count": 0,
                "lifecycle_count": 0,
                "segment_state_count": 0,
                "reindex_count": 0,
                "production_full_count": 0,
            }
            records.append(record)
            key = f"{record['reassessment']}::{record['issue_family']}::{record['issue_kind']}"
            if len(samples[key]) < 5:
                samples[key].append(
                    {
                        "segment_id": segment_id,
                        "relative_path": record["relative_path"],
                        "source_key": record["source_key"],
                        "human_decision": record["human_decision"],
                        "issue_family": record["issue_family"],
                        "issue_kind": record["issue_kind"],
                        "issue_severity": record["issue_severity"],
                        "output_text": record["output_text"],
                    }
                )

    issue_records = [record for record in records if record["record_type"] == "reviewed_segment_open_issue_reassessment"]
    segment_level_action: dict[int, Counter[str]] = defaultdict(Counter)
    for record in issue_records:
        segment_level_action[int(record["segment_id"])][str(record["recommended_action"])] += 1
    segment_disposition = {}
    for segment_id, actions in segment_level_action.items():
        if actions.get("keep_issue_open"):
            segment_disposition[segment_id] = "keep_open_or_parser_hold"
        elif actions.get("architecture_or_parser_review"):
            segment_disposition[segment_id] = "parser_or_architecture_review"
        elif actions.get("human_issue_triage_before_closure"):
            segment_disposition[segment_id] = "human_issue_triage"
        elif actions.get("candidate_for_issue_closure_dry_run"):
            segment_disposition[segment_id] = "candidate_for_issue_closure_dry_run"
        else:
            segment_disposition[segment_id] = "manual_issue_review"

    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_reviewed_issue_ledger_diagnostic",
        "run_id": args.run_id,
        "issue_ledger_run_id": ledger_run_id,
        "batch_jsonls": [str(path) for path in batch_paths],
        "reviewed_segment_count": len(segment_ids),
        "open_issue_record_count": len(issue_records),
        "segments_with_open_issue_count": len(segment_level_action),
        "human_decision_counts": dict(Counter(str(decisions[sid].get("human_decision")) for sid in segment_ids).most_common()),
        "issue_family_counts": dict(Counter(str(record["issue_family"]) for record in issue_records).most_common(50)),
        "issue_kind_counts": dict(Counter(str(record["issue_kind"]) for record in issue_records).most_common(50)),
        "issue_severity_counts": dict(Counter(str(record["issue_severity"]) for record in issue_records).most_common()),
        "reassessment_counts": dict(Counter(str(record["reassessment"]) for record in issue_records).most_common()),
        "recommended_action_counts": dict(Counter(str(record["recommended_action"]) for record in issue_records).most_common()),
        "segment_disposition_counts": dict(Counter(segment_disposition.values()).most_common()),
        "candidate_for_issue_closure_segment_ids": sorted(
            segment_id for segment_id, disposition in segment_disposition.items() if disposition == "candidate_for_issue_closure_dry_run"
        ),
        "human_issue_triage_segment_ids": sorted(
            segment_id for segment_id, disposition in segment_disposition.items() if disposition == "human_issue_triage"
        ),
        "parser_or_hold_segment_ids": sorted(
            segment_id
            for segment_id, disposition in segment_disposition.items()
            if disposition in {"keep_open_or_parser_hold", "parser_or_architecture_review"}
        ),
        "samples_by_reassessment": dict(samples),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "single_operational_recommendation": (
            "Do not close issues yet. Prepare a separate issue-closure dry-run only for likely_resolved_by_human_review rows, "
            "keep parser holds open, and manually triage high_issue_auditor rows before any ledger status changes."
        ),
    }
    return records, summary


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_release_readiness_ui_tooltips_reviewed_issue_ledger_diagnostic"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    txt_path.write_text(
        "\n".join(
            [
                "Release readiness UI/tooltips reviewed issue-ledger diagnostic",
                f"run_id={summary['run_id']}",
                f"reviewed_segment_count={summary['reviewed_segment_count']}",
                f"open_issue_record_count={summary['open_issue_record_count']}",
                f"segments_with_open_issue_count={summary['segments_with_open_issue_count']}",
                f"human_decision_counts={json.dumps(summary['human_decision_counts'], ensure_ascii=False, sort_keys=True)}",
                f"issue_family_counts={json.dumps(summary['issue_family_counts'], ensure_ascii=False, sort_keys=True)}",
                f"issue_severity_counts={json.dumps(summary['issue_severity_counts'], ensure_ascii=False, sort_keys=True)}",
                f"reassessment_counts={json.dumps(summary['reassessment_counts'], ensure_ascii=False, sort_keys=True)}",
                f"recommended_action_counts={json.dumps(summary['recommended_action_counts'], ensure_ascii=False, sort_keys=True)}",
                f"segment_disposition_counts={json.dumps(summary['segment_disposition_counts'], ensure_ascii=False, sort_keys=True)}",
                "candidate_generation_count=0",
                "apply_count=0",
                "lifecycle_count=0",
                "segment_state_count=0",
                "reindex_count=0",
                "production_full_count=0",
                "",
                "Recommendation:",
                summary["single_operational_recommendation"],
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return txt_path, jsonl_path, summary_path


def main() -> None:
    args = parse_args()
    records, summary = build(args)
    txt_path, jsonl_path, summary_path = write_reports(records, summary)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"reviewed_segment_count={summary['reviewed_segment_count']}")
    print(f"open_issue_record_count={summary['open_issue_record_count']}")
    print(f"segments_with_open_issue_count={summary['segments_with_open_issue_count']}")
    print(f"reassessment_counts={summary['reassessment_counts']}")
    print(f"recommended_action_counts={summary['recommended_action_counts']}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
