from __future__ import annotations

import json
import sqlite3
import argparse
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import segment_state_snapshot


SOURCE = "domain_policy_vote_candidate_lifecycle_policy_consumption_debug_v1"
ARCHITECTURE_PACKET_JSONL = Path("reports/20260701_020828_439617_domain_policy_vote_candidate_closure_debt_architecture_packet.jsonl")
BASE_RUN_ID = 526
CURRENT_RUN_ID = 528
PREVIOUS_SUMMARY = Path("reports/20260701_023425_046180_domain_policy_vote_candidate_lifecycle_policy_consumption_debug_371_summary.json")
SAMPLE_LIMIT = 8


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only lifecycle policy consumption debug.")
    parser.add_argument("--architecture-packet-jsonl", type=Path, default=ARCHITECTURE_PACKET_JSONL)
    parser.add_argument("--base-run-id", type=int, default=BASE_RUN_ID)
    parser.add_argument("--current-run-id", type=int, default=CURRENT_RUN_ID)
    parser.add_argument("--previous-summary", type=Path, default=PREVIOUS_SUMMARY)
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
    return conn


def actions(value: str | None) -> list[str]:
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def fetch_current_rows(conn: sqlite3.Connection, segment_ids: list[int], current_run_id: int) -> dict[int, dict[str, Any]]:
    output: dict[int, dict[str, Any]] = {}
    for index in range(0, len(segment_ids), 800):
        chunk = segment_ids[index : index + 800]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""
            SELECT
                state.segment_id,
                state.final_state,
                state.state_group,
                state.active_action,
                state.candidate_action,
                state.policy_action,
                state.confirmed_matches_output,
                state.needs_output_apply,
                state.lifecycle_policy_allowed,
                state.lifecycle_policy_action,
                state.reasons_json,
                state.is_closed,
                state.needs_human,
                state.needs_reopen
            FROM segment_state_items state
            WHERE state.run_id = ?
              AND state.segment_id IN ({placeholders})
            """,
            (current_run_id, *chunk),
        ).fetchall()
        for row in rows:
            output[int(row["segment_id"])] = dict(row)
    return output


def fetch_policy_items(conn: sqlite3.Connection, segment_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    output: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for index in range(0, len(segment_ids), 800):
        chunk = segment_ids[index : index + 800]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""
            SELECT
                item.segment_id,
                run.id AS policy_run_id,
                run.policy_name,
                run.policy_status,
                item.policy_action,
                item.policy_allowed,
                item.block_reason,
                item.issue_count,
                item.high_issue_count,
                item.output_match_kind,
                item.token_status
            FROM auto_confirmation_reopen_lifecycle_policy_items item
            JOIN auto_confirmation_reopen_lifecycle_policy_runs run ON run.id = item.run_id
            WHERE item.segment_id IN ({placeholders})
              AND item.policy_allowed = 1
            ORDER BY item.segment_id, run.id DESC, item.id DESC
            """,
            tuple(chunk),
        ).fetchall()
        for row in rows:
            output[int(row["segment_id"])].append(dict(row))
    return output


def root_cause(record: dict[str, Any], current: dict[str, Any], policy_items: list[dict[str, Any]]) -> str:
    open_issue_count = int(record.get("open_issue_count") or 0)
    high_issue_count = int(record.get("high_issue_count") or 0)
    current_action_list = actions(current.get("lifecycle_policy_action") or record.get("lifecycle_policy_action"))
    mapped = [action for action in current_action_list if action in segment_state_snapshot.LIFECYCLE_CLOSURE_ACTIONS]
    unmapped = [action for action in current_action_list if action not in segment_state_snapshot.LIFECYCLE_CLOSURE_ACTIONS]
    current_final = str(current.get("final_state") or "")
    if high_issue_count > 0:
        return "blocked_by_open_high_issue"
    if open_issue_count > 0:
        return "blocked_by_open_issue"
    if not current_action_list:
        return "lifecycle_policy_action_incomplete"
    if not mapped:
        return "policy_action_present_but_not_in_lifecycle_closure_actions"
    if unmapped and mapped:
        return "mixed_policy_actions_some_not_mapped"
    if current_final != "reopen_auto_confirmed_autofix":
        return "already_moved_or_no_longer_in_focus"
    if any(str(item.get("block_reason") or "").strip() for item in policy_items):
        return "policy_item_has_block_reason_despite_allowed"
    if str(record.get("from_final_state") or "") and str(record.get("from_final_state")) not in current_final:
        return "mapped_action_but_fast_path_or_state_guard_missing"
    return "mapped_action_but_overridden_by_other_guard"


def recommendation_for(cause: str) -> str:
    if cause == "blocked_by_open_high_issue":
        return "hold; resolve or explicitly cover high issues before any lifecycle closure"
    if cause == "blocked_by_open_issue":
        return "hold or create issue-aware bridge only if architecture validates the issue family coverage"
    if cause == "lifecycle_policy_action_incomplete":
        return "fix/materialize policy item action before segment-state can consume it"
    if cause == "policy_action_present_but_not_in_lifecycle_closure_actions":
        return "architecture must map policy_action into LIFECYCLE_CLOSURE_ACTIONS and a closed final_state"
    if cause == "mixed_policy_actions_some_not_mapped":
        return "normalize/split lifecycle actions so all allowed actions have deterministic closure mapping"
    if cause == "mapped_action_but_fast_path_or_state_guard_missing":
        return "architecture should inspect fast path/state guard ordering for this policy family"
    if cause == "mapped_action_but_overridden_by_other_guard":
        return "architecture should inspect classify guard precedence; do not rerun lifecycle blindly"
    return "no action; out of current focus"


def compact(
    record: dict[str, Any],
    current: dict[str, Any],
    policy_items: list[dict[str, Any]],
    base_run_id: int,
    current_run_id: int,
) -> dict[str, Any]:
    current_action_list = actions(current.get("lifecycle_policy_action") or record.get("lifecycle_policy_action"))
    mapped = [action for action in current_action_list if action in segment_state_snapshot.LIFECYCLE_CLOSURE_ACTIONS]
    unmapped = [action for action in current_action_list if action not in segment_state_snapshot.LIFECYCLE_CLOSURE_ACTIONS]
    cause = root_cause(record, current, policy_items)
    return {
        "source": SOURCE,
        "segment_id": int(record["segment_id"]),
        "relative_path": record.get("relative_path"),
        "source_key": record.get("source_key"),
        "phase": record.get("phase"),
        "base_run_id": base_run_id,
        "current_run_id": current_run_id,
        "from_final_state": record.get("from_final_state"),
        "base_to_final_state": record.get("to_final_state"),
        "current_final_state": current.get("final_state"),
        "current_state_group": current.get("state_group"),
        "confirmed_matches_output": int(current.get("confirmed_matches_output") or 0),
        "needs_output_apply": int(current.get("needs_output_apply") or 0),
        "lifecycle_policy_allowed": int(current.get("lifecycle_policy_allowed") or record.get("lifecycle_policy_allowed") or 0),
        "lifecycle_policy_action": current.get("lifecycle_policy_action") or record.get("lifecycle_policy_action"),
        "mapped_lifecycle_actions": mapped,
        "unmapped_lifecycle_actions": unmapped,
        "active_action": current.get("active_action"),
        "candidate_action": current.get("candidate_action"),
        "policy_action": current.get("policy_action"),
        "open_issue_count": int(record.get("open_issue_count") or 0),
        "high_issue_count": int(record.get("high_issue_count") or 0),
        "issue_families": record.get("issue_families"),
        "issue_kinds": record.get("issue_kinds"),
        "token_surface": record.get("token_surface"),
        "policy_item_count": len(policy_items),
        "policy_items": policy_items[:5],
        "root_cause": cause,
        "recommendation": recommendation_for(cause),
        "should_hold": cause.startswith("blocked_by") or cause == "policy_item_has_block_reason_despite_allowed",
        "needs_architecture_change": cause in {
            "policy_action_present_but_not_in_lifecycle_closure_actions",
            "mixed_policy_actions_some_not_mapped",
            "mapped_action_but_fast_path_or_state_guard_missing",
            "mapped_action_but_overridden_by_other_guard",
            "lifecycle_policy_action_incomplete",
        },
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
    }


def read_previous_summary(path: Path) -> dict[str, Any] | None:
    full_path = db.project_path(path)
    if not full_path.exists():
        return None
    with full_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def build(
    records: list[dict[str, Any]],
    architecture_packet_jsonl: Path,
    base_run_id: int,
    current_run_id: int,
    previous_summary_path: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    focus = [
        row
        for row in records
        if row.get("phase") == "debug_existing_policy_consumption"
        and int(row.get("lifecycle_policy_allowed") or 0) == 1
        and row.get("to_final_state") == "reopen_auto_confirmed_autofix"
        and int(row.get("confirmed_matches_output") or 0) == 1
        and int(row.get("needs_output_apply") or 0) == 0
    ]
    segment_ids = [int(row["segment_id"]) for row in focus]
    with connect_readonly() as conn:
        current_rows = fetch_current_rows(conn, segment_ids, current_run_id)
        policy_items = fetch_policy_items(conn, segment_ids)
    output = [
        compact(
            row,
            current_rows.get(int(row["segment_id"]), {}),
            policy_items.get(int(row["segment_id"]), []),
            base_run_id,
            current_run_id,
        )
        for row in focus
    ]
    cause_counts = Counter(row["root_cause"] for row in output)
    action_counts = Counter(str(row["lifecycle_policy_action"] or "") for row in output)
    mapped_counts = Counter(",".join(row["mapped_lifecycle_actions"]) or "none" for row in output)
    issue_family_counts = Counter(str(row["issue_families"] or "none") for row in output)
    samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in output:
        if len(samples[row["root_cause"]]) < SAMPLE_LIMIT:
            samples[row["root_cause"]].append(row)
    previous_summary = read_previous_summary(previous_summary_path)
    previous_root_counts = dict((previous_summary or {}).get("root_cause_counts") or {})
    root_count_delta = {
        key: cause_counts.get(key, 0) - int(previous_root_counts.get(key, 0) or 0)
        for key in sorted(set(cause_counts) | set(previous_root_counts))
    }
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_lifecycle_policy_consumption_debug",
        "input_jsonl": str(architecture_packet_jsonl),
        "base_run_id": base_run_id,
        "current_run_id": current_run_id,
        "previous_summary": str(previous_summary_path),
        "record_count": len(output),
        "expected_focus_count": 371,
        "root_cause_counts": dict(sorted(cause_counts.items())),
        "previous_root_cause_counts": previous_root_counts,
        "root_cause_count_delta_vs_previous": root_count_delta,
        "top_lifecycle_policy_actions": dict(action_counts.most_common(30)),
        "mapped_lifecycle_action_counts": dict(mapped_counts.most_common(30)),
        "top_issue_family_counts": dict(issue_family_counts.most_common(20)),
        "needs_architecture_change_count": sum(1 for row in output if row["needs_architecture_change"]),
        "hold_count": sum(1 for row in output if row["should_hold"]),
        "samples_by_root_cause": samples,
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
            "Do not rerun lifecycle. First resolve high/open issue blockers and ask architecture to map or debug policy actions that are allowed but not consumed."
        ),
    }
    return output, summary


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_lifecycle_policy_consumption_debug_371"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "Lifecycle policy consumption debug for closure debt",
        f"record_count={summary['record_count']}",
        "",
        "Root cause counts:",
    ]
    for key, count in summary["root_cause_counts"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(["", "Top lifecycle_policy_action:"])
    for key, count in summary["top_lifecycle_policy_actions"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(
        [
            "",
            f"needs_architecture_change_count={summary['needs_architecture_change_count']}",
            f"hold_count={summary['hold_count']}",
            "",
            "Guards:",
            "candidate_generation_count=0",
            "apply_count=0",
            "lifecycle_count=0",
            "segment_state_count=0",
            "reindex_count=0",
            "production_full_count=0",
            "source_changed=false",
            "output_changed=false",
            "",
            "Recommendation:",
            summary["single_operational_recommendation"],
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    args = parse_args()
    rows = read_jsonl(args.architecture_packet_jsonl)
    records, summary = build(
        rows,
        args.architecture_packet_jsonl,
        args.base_run_id,
        args.current_run_id,
        args.previous_summary,
    )
    if summary["record_count"] != summary["expected_focus_count"]:
        raise SystemExit(f"focus count guard failed: {summary['record_count']}")
    txt_path, jsonl_path, summary_path = write_reports(records, summary)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"record_count={summary['record_count']}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
