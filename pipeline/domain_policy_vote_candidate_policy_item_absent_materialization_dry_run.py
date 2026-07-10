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


SOURCE = "domain_policy_vote_candidate_policy_item_absent_materialization_dry_run_v1"
INPUT_JSONL = Path("reports/20260701_212710_835988_domain_policy_vote_candidate_policy_item_absent_diagnostic_9.jsonl")
CURRENT_RUN_ID = 535
EXPECTED_COUNT = 9
SAMPLE_LIMIT = 8

BOUNDARY_ACTION = "close_reopen_boundary_token_subpolicy_controlled"
SAME_TOKEN_ACTION = "close_reopen_same_token_boundary_repair_controlled"


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only dry-run for missing lifecycle policy item materialization.")
    parser.add_argument("--input-jsonl", type=Path, default=INPUT_JSONL)
    parser.add_argument("--current-run-id", type=int, default=CURRENT_RUN_ID)
    parser.add_argument("--expected-count", type=int, default=EXPECTED_COUNT)
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


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def canonical_l10n(text: str | None) -> str:
    value = str(text or "")
    value = value.replace("\\\"", '"')
    value = value.replace("\\n", "\n")
    return value.strip()


def token_status_for(text: str | None) -> str:
    value = str(text or "")
    if "[" in value and "]" not in value:
        return "bad_unbalanced_bracket"
    if "]" in value and "[" not in value:
        return "bad_unbalanced_bracket"
    if value.count("#!") > value.count("#"):
        return "bad_marker_balance"
    return "ok"


def fetch_context(conn: sqlite3.Connection, segment_ids: list[int], run_id: int) -> dict[int, dict[str, Any]]:
    if not segment_ids:
        return {}
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT
          s.id AS segment_id,
          s.relative_path,
          s.source_line_number,
          s.source_key,
          st.final_state,
          st.confirmed_matches_output,
          st.needs_output_apply,
          st.lifecycle_policy_allowed,
          st.lifecycle_policy_action,
          c.confirmed_text,
          c.confirmation_label,
          c.confirmation_source,
          c.confirmation_level,
          c.locked,
          o.portuguese_text AS output_text
        FROM source_segments s
        JOIN segment_state_items st ON st.segment_id = s.id AND st.run_id = ?
        LEFT JOIN segment_confirmations c ON c.segment_id = s.id
        LEFT JOIN output_segments o ON o.segment_id = s.id
        WHERE s.id IN ({placeholders})
        """,
        (run_id, *segment_ids),
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def proposed_actions(family: str) -> tuple[str, list[str], str]:
    if family == "missing_boundary_token_subpolicy_controlled_policy_item":
        return (
            "boundary_token_subpolicy_controlled_policy_item_backfill",
            [BOUNDARY_ACTION],
            "single_boundary_action",
        )
    if family == "missing_same_token_boundary_repair_controlled_policy_item":
        return (
            "same_token_boundary_repair_controlled_policy_item_backfill",
            [SAME_TOKEN_ACTION],
            "single_same_token_action",
        )
    if family == "missing_split_mixed_boundary_and_same_token_policy_items":
        return (
            "mixed_boundary_same_token_policy_item_backfill_requires_architecture_choice",
            [BOUNDARY_ACTION, SAME_TOKEN_ACTION],
            "mixed_requires_two_items_or_dominant_action_decision",
        )
    return ("unknown_policy_item_backfill", [], "unknown")


def guard_failures(row: dict[str, Any], context: dict[str, Any], actions: list[str]) -> list[str]:
    failures: list[str] = []
    confirmed = context.get("confirmed_text") or ""
    output = context.get("output_text") or ""
    if int(context.get("confirmed_matches_output") or 0) != 1:
        failures.append("confirmed_matches_output_not_1")
    if int(context.get("needs_output_apply") or 0) != 0:
        failures.append("needs_output_apply_not_0")
    if int(row.get("open_issue_count") or 0) != 0:
        failures.append("open_issue_count_not_0")
    if int(row.get("high_issue_count") or 0) != 0:
        failures.append("high_issue_count_not_0")
    if canonical_l10n(output) != canonical_l10n(confirmed):
        failures.append("canonical_output_confirmed_mismatch")
    if token_status_for(confirmed) != "ok":
        failures.append("token_status_not_ok")
    if context.get("final_state") != "reopen_auto_confirmed_autofix":
        failures.append("current_state_not_reopen_auto_confirmed_autofix")
    if not actions:
        failures.append("missing_proposed_policy_action")
    return failures


def build_record(row: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    family = str(row.get("recommended_policy_family") or "")
    policy_name, actions, decision_mode = proposed_actions(family)
    failures = guard_failures(row, context, actions)
    ready = not failures and decision_mode != "mixed_requires_two_items_or_dominant_action_decision"
    return {
        "source": SOURCE,
        "segment_id": int(row["segment_id"]),
        "relative_path": context.get("relative_path") or row.get("relative_path"),
        "source_key": context.get("source_key") or row.get("source_key"),
        "source_line_number": context.get("source_line_number"),
        "recommended_policy_family": family,
        "proposed_policy_name": policy_name,
        "proposed_policy_actions": actions,
        "decision_mode": decision_mode,
        "materialization_ready": ready,
        "requires_architecture_choice": decision_mode == "mixed_requires_two_items_or_dominant_action_decision",
        "guard_ok": not failures,
        "guard_failures": failures,
        "current_final_state": context.get("final_state"),
        "confirmed_matches_output": int(context.get("confirmed_matches_output") or 0),
        "needs_output_apply": int(context.get("needs_output_apply") or 0),
        "open_issue_count": int(row.get("open_issue_count") or 0),
        "high_issue_count": int(row.get("high_issue_count") or 0),
        "output_match_kind": "canonical_equal" if canonical_l10n(context.get("output_text")) == canonical_l10n(context.get("confirmed_text")) else "different",
        "token_status": token_status_for(context.get("confirmed_text")),
        "confirmation_label": context.get("confirmation_label"),
        "confirmation_source": context.get("confirmation_source"),
        "confirmation_level": context.get("confirmation_level"),
        "locked": int(context.get("locked") or 0),
        "output_text": context.get("output_text"),
        "confirmed_text": context.get("confirmed_text"),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
    }


def build(input_jsonl: Path, current_run_id: int, expected_count: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = read_jsonl(input_jsonl)
    if len(rows) != expected_count:
        raise SystemExit(f"input count guard failed: {len(rows)}")
    segment_ids = [int(row["segment_id"]) for row in rows]
    with connect_readonly() as conn:
        contexts = fetch_context(conn, segment_ids, current_run_id)
    records = [build_record(row, contexts.get(int(row["segment_id"]), {})) for row in rows]
    records.sort(key=lambda row: (row["decision_mode"], row["segment_id"]))

    family_counts = Counter(row["recommended_policy_family"] for row in records)
    decision_counts = Counter(row["decision_mode"] for row in records)
    action_counts = Counter(",".join(row["proposed_policy_actions"]) for row in records)
    readiness_counts = Counter("ready" if row["materialization_ready"] else "not_ready" for row in records)
    guard_counts = Counter("guard_ok" if row["guard_ok"] else "guard_failed" for row in records)
    failure_counts = Counter(failure for row in records for failure in row["guard_failures"])
    samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in records:
        if len(samples[row["decision_mode"]]) < SAMPLE_LIMIT:
            samples[row["decision_mode"]].append(row)

    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_policy_item_absent_materialization_dry_run",
        "input_jsonl": str(input_jsonl),
        "current_run_id": current_run_id,
        "record_count": len(records),
        "expected_record_count": expected_count,
        "recommended_policy_family_counts": dict(family_counts.most_common()),
        "decision_mode_counts": dict(decision_counts.most_common()),
        "proposed_policy_action_counts": dict(action_counts.most_common()),
        "readiness_counts": dict(readiness_counts.most_common()),
        "guard_counts": dict(guard_counts.most_common()),
        "guard_failure_counts": dict(failure_counts.most_common()),
        "materialization_ready_count": sum(1 for row in records if row["materialization_ready"]),
        "requires_architecture_choice_count": sum(1 for row in records if row["requires_architecture_choice"]),
        "samples_by_decision_mode": samples,
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
            "Do not materialize yet. The ready single-action rows can be materialized after explicit approval; "
            "the mixed row needs architecture to choose two policy items or a dominant action."
        ),
    }
    return records, summary


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_policy_item_absent_materialization_dry_run_9"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "Policy item absent materialization dry-run",
        f"record_count={summary['record_count']}",
        f"materialization_ready_count={summary['materialization_ready_count']}",
        f"requires_architecture_choice_count={summary['requires_architecture_choice_count']}",
        "",
        "Decision modes:",
    ]
    for key, count in summary["decision_mode_counts"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(["", "Guards:"])
    for key, count in summary["guard_counts"].items():
        lines.append(f"- {key}: {count}")
    if summary["guard_failure_counts"]:
        lines.extend(["", "Guard failures:"])
        for key, count in summary["guard_failure_counts"].items():
            lines.append(f"- {key}: {count}")
    lines.extend(
        [
            "",
            "Operation counters:",
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
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    args = parse_args()
    records, summary = build(args.input_jsonl, args.current_run_id, args.expected_count)
    txt, jsonl, summary_path = write_reports(records, summary)
    print(f"txt={txt}")
    print(f"jsonl={jsonl}")
    print(f"summary={summary_path}")
    print(f"record_count={summary['record_count']}")
    print(f"materialization_ready_count={summary['materialization_ready_count']}")
    print(f"requires_architecture_choice_count={summary['requires_architecture_choice_count']}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
