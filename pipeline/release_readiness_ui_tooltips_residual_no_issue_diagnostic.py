from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_safe_output_updates import protected_tokens
from apply_segment_state_updates import canonical_localization_text


SOURCE = "release_readiness_ui_tooltips_residual_no_issue_diagnostic_v1"
DEFAULT_RUN_ID = 548
DEFAULT_LEDGER_RUN_ID = 76
DEFAULT_SEGMENT_IDS = [
    4184,
    35050,
    35493,
    38526,
    46892,
    47168,
    49017,
    49487,
    49928,
    53322,
    58495,
    62803,
    62843,
    74518,
]


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
    parser = argparse.ArgumentParser(description="Read-only diagnostic for UI/tooltips residual no-issue reviewed segments.")
    parser.add_argument("--run-id", type=int, default=DEFAULT_RUN_ID)
    parser.add_argument("--ledger-run-id", type=int, default=DEFAULT_LEDGER_RUN_ID)
    parser.add_argument("--segment-id", type=int, action="append", dest="segment_ids")
    return parser.parse_args()


def token_surface(text: str | None) -> str:
    text = text or ""
    tokens = protected_tokens(text)
    if "\n" in text or "\\n" in text:
        return "multiline"
    if not tokens:
        return "plain_text"
    blob = " ".join(tokens)
    if "Select_CString" in blob or "SelectLocalization" in blob:
        return "dynamic_select"
    if any(marker in blob for marker in [".Get", ".Custom", "ROOT.", "scope:", "SCOPE.", "GetScriptValue"]):
        return "dynamic_getter"
    return "light_token"


def canonical_equal(left: str | None, right: str | None) -> bool:
    return canonical_localization_text(left or "") == canonical_localization_text(right or "")


def fetch_issue_counts(conn: sqlite3.Connection, ledger_run_id: int, segment_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not segment_ids:
        return {}
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT
          segment_id,
          COUNT(*) AS issue_count,
          SUM(CASE WHEN lower(COALESCE(issue_severity,'')) IN ('high','critical','error') THEN 1 ELSE 0 END) AS high_issue_count,
          GROUP_CONCAT(DISTINCT issue_family) AS issue_families,
          GROUP_CONCAT(DISTINCT issue_kind) AS issue_kinds
        FROM ml_issue_ledger_items
        WHERE run_id = ?
          AND segment_id IN ({placeholders})
          AND COALESCE(status, 'open') NOT IN ('closed', 'resolved', 'dismissed')
        GROUP BY segment_id
        """,
        [ledger_run_id, *segment_ids],
    ).fetchall()
    return {
        int(row["segment_id"]): {
            "issue_count": int(row["issue_count"] or 0),
            "high_issue_count": int(row["high_issue_count"] or 0),
            "issue_families": row["issue_families"] or "",
            "issue_kinds": row["issue_kinds"] or "",
        }
        for row in rows
    }


def policy_bucket(row: dict[str, Any]) -> str:
    policy = row.get("policy_action") or ""
    active = row.get("active_action") or ""
    if policy in {"needs_autofix", "needs_human", "blocked_structure", "auto_safe"}:
        return policy
    if active in {"needs_autofix", "needs_human", "blocked_structure", "auto_safe"}:
        return active
    if "blocked" in policy or "blocked" in active:
        return "blocked_structure"
    if "human" in policy or "human" in active:
        return "needs_human"
    if "autofix" in policy or "autofix" in active:
        return "needs_autofix"
    if "safe" in policy or "safe" in active:
        return "auto_safe"
    return "other"


def disposition(row: dict[str, Any]) -> tuple[str, list[str]]:
    reasons: list[str] = []
    confirmation_level = row.get("confirmation_level") or "none"
    locked = int(row.get("locked") or 0) == 1
    matches_state = int(row.get("confirmed_matches_output") or 0) == 1
    needs_apply = int(row.get("needs_output_apply") or 0) == 1
    issue_count = int(row.get("issue_count") or 0)
    high_issue_count = int(row.get("high_issue_count") or 0)
    canonical_ok = bool(row.get("canonical_output_equals_confirmed"))
    surface = row.get("token_surface")
    bucket = row.get("policy_bucket")

    if needs_apply or not canonical_ok or not matches_state:
        reasons.append("output_not_aligned_with_confirmed_or_state")
        return "needs_protected_apply", reasons
    if issue_count or high_issue_count:
        reasons.append("open_issue_still_present")
        return "hold_autofix", reasons
    if surface in {"dynamic_select", "dynamic_getter", "multiline"}:
        reasons.append(f"non_plain_surface:{surface}")
        return "hold_parser_later", reasons
    if confirmation_level == "human_confirmed" and locked:
        reasons.append("human_confirmed_locked_equal_output_no_issue")
        return "ready_for_lifecycle_bridge", reasons
    if confirmation_level == "human_confirmed":
        reasons.append("human_confirmed_equal_output_no_issue")
        return "ready_for_lifecycle_bridge", reasons
    if bucket == "needs_human":
        reasons.append("state_still_requests_human_review")
        return "needs_human_confirmation", reasons
    if confirmation_level == "auto_confirmed":
        reasons.append("auto_confirmed_equal_output_no_issue_but_autofix_signal")
        return "needs_human_confirmation", reasons
    reasons.append("no_human_confirmation")
    return "needs_human_confirmation", reasons


def fetch_records(conn: sqlite3.Connection, run_id: int, ledger_run_id: int, segment_ids: list[int]) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT
          state.segment_id,
          state.relative_path,
          state.source_key,
          state.source_line_number,
          state.final_state,
          state.state_group,
          state.output_state,
          state.review_state,
          state.apply_state,
          state.active_action,
          state.candidate_action,
          state.policy_action,
          state.confirmation_level,
          state.confirmation_label,
          state.locked,
          state.confirmed_matches_output,
          state.needs_output_apply,
          state.is_closed,
          state.reasons_json,
          state.lifecycle_policy_action,
          state.lifecycle_policy_allowed,
          src.spanish_text,
          src.english_text,
          output.portuguese_text AS output_text,
          conf.confirmed_text,
          conf.confirmation_source
        FROM segment_state_items state
        JOIN source_segments src ON src.id = state.segment_id
        LEFT JOIN output_segments output ON output.segment_id = state.segment_id
        LEFT JOIN segment_confirmations conf ON conf.segment_id = state.segment_id
        WHERE state.run_id = ?
          AND state.segment_id IN ({placeholders})
        ORDER BY state.segment_id
        """,
        [run_id, *segment_ids],
    ).fetchall()
    issue_counts = fetch_issue_counts(conn, ledger_run_id, segment_ids)
    records: list[dict[str, Any]] = []
    for row in rows:
        record = dict(row)
        segment_id = int(record["segment_id"])
        record.update(
            issue_counts.get(
                segment_id,
                {"issue_count": 0, "high_issue_count": 0, "issue_families": "", "issue_kinds": ""},
            )
        )
        record["token_surface"] = token_surface(record.get("output_text") or record.get("confirmed_text") or "")
        record["policy_bucket"] = policy_bucket(record)
        record["canonical_output_equals_confirmed"] = canonical_equal(record.get("output_text"), record.get("confirmed_text"))
        record["raw_output_equals_confirmed"] = (record.get("output_text") or "") == (record.get("confirmed_text") or "")
        try:
            record["state_reasons"] = json.loads(record.get("reasons_json") or "[]")
        except json.JSONDecodeError:
            record["state_reasons"] = ["invalid_reasons_json"]
        record.pop("reasons_json", None)
        decision, reasons = disposition(record)
        record["recommended_disposition"] = decision
        record["disposition_reasons"] = reasons
        record["candidate_generation_count"] = 0
        record["apply_count"] = 0
        record["lifecycle_count"] = 0
        record["segment_state_count"] = 0
        record["reindex_count"] = 0
        record["production_full_count"] = 0
        records.append(record)
    return records


def build_summary(records: list[dict[str, Any]], args: argparse.Namespace) -> dict[str, Any]:
    missing = sorted(set(args.segment_ids or DEFAULT_SEGMENT_IDS) - {int(row["segment_id"]) for row in records})
    human_locked = [
        int(row["segment_id"])
        for row in records
        if row.get("confirmation_level") == "human_confirmed" and int(row.get("locked") or 0) == 1
    ]
    auto_confirmed = [int(row["segment_id"]) for row in records if row.get("confirmation_level") == "auto_confirmed"]
    no_confirmation = [int(row["segment_id"]) for row in records if not row.get("confirmation_level")]
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_diagnostic",
        "segment_state_run_id": args.run_id,
        "ledger_run_id": args.ledger_run_id,
        "requested_segment_count": len(args.segment_ids or DEFAULT_SEGMENT_IDS),
        "record_count": len(records),
        "missing_segment_ids": missing,
        "confirmation_level_counts": dict(Counter(row.get("confirmation_level") or "none" for row in records).most_common()),
        "human_confirmed_locked_count": len(human_locked),
        "human_confirmed_locked_segment_ids": human_locked,
        "auto_confirmed_count": len(auto_confirmed),
        "auto_confirmed_segment_ids": auto_confirmed,
        "no_confirmation_count": len(no_confirmation),
        "no_confirmation_segment_ids": no_confirmation,
        "policy_bucket_counts": dict(Counter(row["policy_bucket"] for row in records).most_common()),
        "active_action_counts": dict(Counter(row.get("active_action") or "none" for row in records).most_common()),
        "policy_action_counts": dict(Counter(row.get("policy_action") or "none" for row in records).most_common()),
        "final_state_counts": dict(Counter(row["final_state"] for row in records).most_common()),
        "token_surface_counts": dict(Counter(row["token_surface"] for row in records).most_common()),
        "confirmed_matches_output_count": sum(1 for row in records if int(row.get("confirmed_matches_output") or 0) == 1),
        "canonical_output_equals_confirmed_count": sum(1 for row in records if row.get("canonical_output_equals_confirmed")),
        "raw_output_equals_confirmed_count": sum(1 for row in records if row.get("raw_output_equals_confirmed")),
        "needs_output_apply_count": sum(1 for row in records if int(row.get("needs_output_apply") or 0) == 1),
        "issue_count_total": sum(int(row.get("issue_count") or 0) for row in records),
        "high_issue_count_total": sum(int(row.get("high_issue_count") or 0) for row in records),
        "recommended_disposition_counts": dict(Counter(row["recommended_disposition"] for row in records).most_common()),
        "ready_for_lifecycle_bridge_segment_ids": [
            int(row["segment_id"]) for row in records if row["recommended_disposition"] == "ready_for_lifecycle_bridge"
        ],
        "needs_human_confirmation_segment_ids": [
            int(row["segment_id"]) for row in records if row["recommended_disposition"] == "needs_human_confirmation"
        ],
        "needs_protected_apply_segment_ids": [
            int(row["segment_id"]) for row in records if row["recommended_disposition"] == "needs_protected_apply"
        ],
        "hold_autofix_segment_ids": [
            int(row["segment_id"]) for row in records if row["recommended_disposition"] == "hold_autofix"
        ],
        "hold_parser_later_segment_ids": [
            int(row["segment_id"]) for row in records if row["recommended_disposition"] == "hold_parser_later"
        ],
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "single_operational_recommendation": (
            "Materialize nothing yet. The only direct lifecycle-bridge candidate is the human-confirmed locked residual; "
            "the auto-confirmed residuals should receive a small human confirmation/triage pass before any close bridge."
        ),
    }
    return summary


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_release_readiness_ui_tooltips_residual_no_issue_diagnostic"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "UI/tooltips residual no-issue diagnostic",
        f"segment_state_run_id={summary['segment_state_run_id']}",
        f"ledger_run_id={summary['ledger_run_id']}",
        f"record_count={summary['record_count']}",
        f"confirmation_level_counts={json.dumps(summary['confirmation_level_counts'], ensure_ascii=False, sort_keys=True)}",
        f"policy_bucket_counts={json.dumps(summary['policy_bucket_counts'], ensure_ascii=False, sort_keys=True)}",
        f"token_surface_counts={json.dumps(summary['token_surface_counts'], ensure_ascii=False, sort_keys=True)}",
        f"recommended_disposition_counts={json.dumps(summary['recommended_disposition_counts'], ensure_ascii=False, sort_keys=True)}",
        f"ready_for_lifecycle_bridge_segment_ids={summary['ready_for_lifecycle_bridge_segment_ids']}",
        f"needs_human_confirmation_segment_ids={summary['needs_human_confirmation_segment_ids']}",
        f"needs_protected_apply_segment_ids={summary['needs_protected_apply_segment_ids']}",
        f"hold_autofix_segment_ids={summary['hold_autofix_segment_ids']}",
        f"hold_parser_later_segment_ids={summary['hold_parser_later_segment_ids']}",
        f"issue_count_total={summary['issue_count_total']}",
        f"high_issue_count_total={summary['high_issue_count_total']}",
        "candidate_generation_count=0",
        "apply_count=0",
        "lifecycle_count=0",
        "segment_state_count=0",
        "reindex_count=0",
        "production_full_count=0",
        "",
        "Records:",
    ]
    for row in records:
        lines.append(
            " - "
            + f"{row['segment_id']}: {row['recommended_disposition']} | "
            + f"confirmation={row.get('confirmation_level')}/{row.get('confirmation_label')} locked={row.get('locked')} | "
            + f"policy={row.get('policy_action')} active={row.get('active_action')} | "
            + f"surface={row.get('token_surface')} | final_state={row.get('final_state')}"
        )
    lines.append("")
    lines.append(f"recommendation={summary['single_operational_recommendation']}")
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    args = parse_args()
    segment_ids = args.segment_ids or DEFAULT_SEGMENT_IDS
    with connect_readonly() as conn:
        records = fetch_records(conn, args.run_id, args.ledger_run_id, segment_ids)
    summary = build_summary(records, args)
    txt_path, jsonl_path, summary_path = write_reports(records, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")


if __name__ == "__main__":
    main()
