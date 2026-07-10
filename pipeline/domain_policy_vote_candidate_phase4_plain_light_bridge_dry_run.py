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


SOURCE = "domain_policy_vote_candidate_phase4_plain_light_bridge_dry_run_v1"
INPUT_JSONL = Path("reports/20260702_004603_514470_domain_policy_vote_candidate_debug_consumption_phase4_readonly_review_run538.jsonl")
RUN_ID = 538
EXPECTED_RELEASED = 115
EXCLUDED_SEGMENT_IDS = {123028}
POLICY_NAME = "auto_confirmed_plain_light_equal_output_lifecycle_bridge"
POLICY_ACTION = "close_reopen_auto_confirmed_plain_light_equal_output_lifecycle"
TARGET_FINAL_STATE = "closed_auto_confirmed_plain_light_equal_output_lifecycle"


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only dry-run for phase 4 plain/light equal-output lifecycle bridge.")
    parser.add_argument("--input-jsonl", type=Path, default=INPUT_JSONL)
    parser.add_argument("--run-id", type=int, default=RUN_ID)
    parser.add_argument("--expected-released", type=int, default=EXPECTED_RELEASED)
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


def token_surface(text: str | None) -> str:
    tokens = protected_tokens(text or "")
    if not tokens:
        return "plain_text"
    token_blob = " ".join(tokens)
    if "\\n" in tokens or "\n" in (text or ""):
        return "multiline"
    if "Select_CString" in token_blob or "SelectLocalization" in token_blob:
        return "dynamic_select"
    if any(part in token_blob for part in [".Get", ".Custom", "ROOT.", "scope:", "SCOPE.", "GetScriptValue"]):
        return "dynamic_getter"
    return "light_token"


def has_dynamic_or_multiline(text: str | None) -> bool:
    tokens = protected_tokens(text or "")
    token_blob = " ".join(tokens)
    return (
        "\\n" in tokens
        or "\n" in (text or "")
        or "Select_CString" in token_blob
        or "SelectLocalization" in token_blob
        or any(part in token_blob for part in [".Get", ".Custom", "ROOT.", "scope:", "SCOPE.", "GetScriptValue"])
    )


def fetch_current(conn: sqlite3.Connection, run_id: int, segment_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not segment_ids:
        return {}
    out: dict[int, dict[str, Any]] = {}
    for start in range(0, len(segment_ids), 800):
        chunk = segment_ids[start : start + 800]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""
            SELECT
                state.segment_id,
                state.relative_path,
                state.source_key,
                state.source_line_number,
                state.final_state,
                state.confirmed_matches_output,
                state.needs_output_apply,
                state.confirmation_level,
                state.confirmation_label,
                state.locked,
                output.portuguese_text AS output_text,
                conf.confirmed_text,
                conf.confirmation_source
            FROM segment_state_items state
            LEFT JOIN output_segments output ON output.segment_id = state.segment_id
            LEFT JOIN segment_confirmations conf ON conf.segment_id = state.segment_id
            WHERE state.run_id = ? AND state.segment_id IN ({placeholders})
            """,
            (run_id, *chunk),
        ).fetchall()
        for row in rows:
            out[int(row["segment_id"])] = dict(row)
    return out


def fetch_open_issue_counts(conn: sqlite3.Connection, segment_ids: list[int]) -> dict[int, dict[str, int]]:
    if not segment_ids:
        return {}
    out: dict[int, dict[str, int]] = {sid: {"open_issue_count": 0, "high_issue_count": 0} for sid in segment_ids}
    for start in range(0, len(segment_ids), 800):
        chunk = segment_ids[start : start + 800]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""
            SELECT
                segment_id,
                COUNT(*) AS open_issue_count,
                SUM(CASE WHEN lower(COALESCE(issue_severity, '')) IN ('high', 'error', 'critical') THEN 1 ELSE 0 END) AS high_issue_count
            FROM ml_issue_ledger_items
            WHERE segment_id IN ({placeholders})
              AND COALESCE(status, 'open') NOT IN ('closed', 'resolved', 'dismissed')
            GROUP BY segment_id
            """,
            tuple(chunk),
        ).fetchall()
        for row in rows:
            out[int(row["segment_id"])] = {
                "open_issue_count": int(row["open_issue_count"] or 0),
                "high_issue_count": int(row["high_issue_count"] or 0),
            }
    return out


def existing_allowed_policy_item(conn: sqlite3.Connection, segment_id: int) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM auto_confirmation_reopen_lifecycle_policy_items
        WHERE segment_id = ?
          AND policy_action = ?
          AND policy_allowed = 1
        """,
        (segment_id, POLICY_ACTION),
    ).fetchone()
    return int(row["count"] or 0)


def validate(record: dict[str, Any], current: dict[str, Any], issue_counts: dict[str, int], conn: sqlite3.Connection) -> list[str]:
    failures: list[str] = []
    segment_id = int(record["segment_id"])
    output_text = current.get("output_text")
    confirmed_text = current.get("confirmed_text")
    surface = token_surface(confirmed_text or output_text)
    if record.get("record_type") != "phase4_candidate_review":
        failures.append("not_phase4_candidate_review")
    if segment_id in EXCLUDED_SEGMENT_IDS:
        failures.append("explicitly_excluded_segment")
    if not bool(record.get("guard_ok")):
        failures.append("previous_guard_not_ok")
    if surface not in {"plain_text", "light_token"}:
        failures.append("not_plain_or_light_token")
    if has_dynamic_or_multiline(confirmed_text or output_text):
        failures.append("dynamic_or_multiline_escaped")
    if current.get("final_state") != "reopen_auto_confirmed_autofix":
        failures.append("final_state_not_reopen_auto_confirmed_autofix")
    if str(current.get("confirmation_level") or "") != "auto_confirmed":
        failures.append("confirmation_level_not_auto_confirmed")
    if canonical_localization_text(output_text) != canonical_localization_text(confirmed_text):
        failures.append("canonical_l10n_output_confirmed_mismatch")
    if int(current.get("confirmed_matches_output") or 0) != 1:
        failures.append("confirmed_matches_output_not_1")
    if int(current.get("needs_output_apply") or 0) != 0:
        failures.append("needs_output_apply_not_0")
    if int(issue_counts.get("open_issue_count") or 0) != 0:
        failures.append("open_issue_count_not_0")
    if int(issue_counts.get("high_issue_count") or 0) != 0:
        failures.append("high_issue_count_not_0")
    if existing_allowed_policy_item(conn, segment_id):
        failures.append("duplicate_allowed_policy_item")
    return failures


def build(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    input_rows = read_jsonl(args.input_jsonl)
    phase4_rows = [
        row
        for row in input_rows
        if row.get("record_type") == "phase4_candidate_review"
        and int(row.get("segment_id") or 0) not in EXCLUDED_SEGMENT_IDS
    ]
    segment_ids = [int(row["segment_id"]) for row in phase4_rows]
    with connect_readonly() as conn:
        current = fetch_current(conn, args.run_id, segment_ids)
        issue_counts = fetch_open_issue_counts(conn, segment_ids)
        records: list[dict[str, Any]] = []
        for row in phase4_rows:
            segment_id = int(row["segment_id"])
            current_row = current.get(segment_id, {})
            counts = issue_counts.get(segment_id, {"open_issue_count": 0, "high_issue_count": 0})
            failures = validate(row, current_row, counts, conn)
            released = not failures
            output_text = current_row.get("output_text")
            confirmed_text = current_row.get("confirmed_text")
            records.append(
                {
                    "source": SOURCE,
                    "record_type": "released" if released else "blocked",
                    "segment_id": segment_id,
                    "relative_path": current_row.get("relative_path") or row.get("relative_path"),
                    "source_key": current_row.get("source_key") or row.get("source_key"),
                    "source_line_number": current_row.get("source_line_number"),
                    "policy_name": POLICY_NAME,
                    "policy_action": POLICY_ACTION,
                    "target_final_state": TARGET_FINAL_STATE,
                    "current_final_state": current_row.get("final_state"),
                    "token_surface": token_surface(confirmed_text or output_text),
                    "confirmation_level": current_row.get("confirmation_level"),
                    "confirmation_source": current_row.get("confirmation_source"),
                    "confirmation_label": current_row.get("confirmation_label"),
                    "canonical_l10n_output_equals_confirmed": int(
                        canonical_localization_text(output_text) == canonical_localization_text(confirmed_text)
                    ),
                    "confirmed_matches_output": int(current_row.get("confirmed_matches_output") or 0),
                    "needs_output_apply": int(current_row.get("needs_output_apply") or 0),
                    "open_issue_count": int(counts.get("open_issue_count") or 0),
                    "high_issue_count": int(counts.get("high_issue_count") or 0),
                    "guard_failures": failures,
                    "policy_allowed": int(released),
                    "candidate_generation_count": 0,
                    "apply_count": 0,
                    "lifecycle_count": 0,
                    "segment_state_count": 0,
                    "reindex_count": 0,
                    "production_full_count": 0,
                }
            )
    status_counts = Counter(record["record_type"] for record in records)
    failure_counts = Counter()
    surface_counts = Counter(str(record["token_surface"]) for record in records)
    label_counts = Counter(str(record["confirmation_label"]) for record in records)
    for record in records:
        for failure in record["guard_failures"]:
            failure_counts[failure] += 1
    released_count = status_counts.get("released", 0)
    blocked_count = status_counts.get("blocked", 0)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_phase4_plain_light_bridge_dry_run",
        "input_jsonl": str(args.input_jsonl),
        "run_id": args.run_id,
        "policy_name": POLICY_NAME,
        "policy_action": POLICY_ACTION,
        "target_final_state": TARGET_FINAL_STATE,
        "excluded_segment_ids": sorted(EXCLUDED_SEGMENT_IDS),
        "record_count": len(records),
        "released_count": released_count,
        "blocked_count": blocked_count,
        "expected_released": args.expected_released,
        "expected_outcome_met": released_count == args.expected_released and blocked_count == 0,
        "token_surface_counts": dict(surface_counts.most_common()),
        "confirmation_label_counts": dict(label_counts.most_common()),
        "guard_failure_counts": dict(failure_counts.most_common()),
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
            "Dry-run passed. Next safe step is to materialize only this narrow bridge, then run only segment-state and delta."
            if released_count == args.expected_released and blocked_count == 0
            else "Dry-run did not pass. Do not materialize; review blocked guards first."
        ),
    }
    return records, summary


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_phase4_plain_light_bridge_dry_run"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "Phase 4 plain/light equal-output lifecycle bridge dry-run",
        f"run_id={summary['run_id']}",
        f"policy_name={summary['policy_name']}",
        f"policy_action={summary['policy_action']}",
        f"target_final_state={summary['target_final_state']}",
        f"record_count={summary['record_count']}",
        f"released_count={summary['released_count']}",
        f"blocked_count={summary['blocked_count']}",
        f"expected_outcome_met={summary['expected_outcome_met']}",
        f"token_surface_counts={json.dumps(summary['token_surface_counts'], ensure_ascii=False, sort_keys=True)}",
        f"guard_failure_counts={json.dumps(summary['guard_failure_counts'], ensure_ascii=False, sort_keys=True)}",
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
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    args = parse_args()
    records, summary = build(args)
    txt_path, jsonl_path, summary_path = write_reports(records, summary)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"released_count={summary['released_count']}")
    print(f"blocked_count={summary['blocked_count']}")
    print(f"expected_outcome_met={summary['expected_outcome_met']}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
