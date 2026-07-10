from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_phase1_human_package_close_bridge_materializer_v1"
DRY_RUN_JSONL = Path("reports/20260701_130652_085046_domain_policy_vote_candidate_phase1_human_package_close_bridge_dry_run.jsonl")
DRY_RUN_SUMMARY = Path("reports/20260701_130652_085046_domain_policy_vote_candidate_phase1_human_package_close_bridge_dry_run_summary.json")
POLICY_NAME = "human_confirmed_package_close_lifecycle_bridge"
POLICY_ACTION = "close_reopen_human_confirmed_package_close_lifecycle"
LABEL_FAMILY = "human_confirmed_package_close"
EXPECTED_COUNT = 1560
SEGMENT_STATE_RUN_ID = 528


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json(path: Path) -> dict[str, Any]:
    with db.project_path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


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


def validate_inputs(summary: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    expected = {
        "record_count": EXPECTED_COUNT,
        "phase1_safety_eligible_count": EXPECTED_COUNT,
        "consumer_supported_now_count": EXPECTED_COUNT,
        "released_count": EXPECTED_COUNT,
        "blocked_count": 0,
    }
    for key, value in expected.items():
        if int(summary.get(key) or 0) != value:
            raise SystemExit(f"summary guard failed: {key}={summary.get(key)} expected {value}")
    if len(rows) != EXPECTED_COUNT:
        raise SystemExit(f"row count guard failed: {len(rows)}")
    for row in rows:
        if row.get("dry_run_decision") != "released":
            raise SystemExit(f"non-released row in dry-run: {row.get('segment_id')}")
        if row.get("recommended_policy_name") != POLICY_NAME:
            raise SystemExit("policy_name guard failed")
        if row.get("recommended_policy_action") != POLICY_ACTION:
            raise SystemExit("policy_action guard failed")
        if row.get("requires_architecture_change_before_apply"):
            raise SystemExit("architecture change guard failed")


def fetch_state_guards(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    ids = [int(row["segment_id"]) for row in rows]
    output: dict[int, dict[str, Any]] = {}
    for index in range(0, len(ids), 800):
        chunk = ids[index : index + 800]
        placeholders = ",".join("?" for _ in chunk)
        result = conn.execute(
            f"""
            SELECT
                state.segment_id,
                state.final_state,
                state.state_group,
                state.confirmed_matches_output,
                state.needs_output_apply,
                state.is_closed,
                state.confirmation_level,
                state.confirmation_label,
                state.locked,
                c.confirmation_source,
                c.confirmed_text,
                o.portuguese_text
            FROM segment_state_items state
            JOIN segment_confirmations c ON c.segment_id = state.segment_id
            JOIN output_segments o ON o.segment_id = state.segment_id
            WHERE state.run_id = ?
              AND state.segment_id IN ({placeholders})
            """,
            (SEGMENT_STATE_RUN_ID, *chunk),
        ).fetchall()
        for row in result:
            output[int(row["segment_id"])] = dict(row)
    if len(output) != len(ids):
        raise SystemExit("state guard row count failed")
    return output


def build_rows(rows: list[dict[str, Any]], state_guards: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    built: list[dict[str, Any]] = []
    for row in rows:
        segment_id = int(row["segment_id"])
        state = state_guards[segment_id]
        block_reasons: list[str] = []
        if state["final_state"] != "reopen_auto_confirmed_autofix" or state["state_group"] != "pending":
            block_reasons.append("current_state_not_pending_reopen")
        if int(state["confirmed_matches_output"] or 0) != 1:
            block_reasons.append("current_confirmed_matches_output_not_1")
        if int(state["needs_output_apply"] or 0) != 0:
            block_reasons.append("current_needs_output_apply_not_0")
        if state["confirmation_level"] != "human_confirmed":
            block_reasons.append("confirmation_not_human_confirmed")
        if state["confirmation_label"] not in {"codex_package_review", "package_close"}:
            block_reasons.append("confirmation_label_not_allowed")
        if state["confirmation_source"] not in {
            "codex_manual",
            "codex_manual+manual_mojibake_cleanup",
            "codex_review",
            "codex_review+manual_mojibake_cleanup",
            "codex_review+manual_mojibake_cleanup+context_mojibake_cleanup",
            "codex_review+manual_mojibake_cleanup+shadow_hygiene_cleanup",
        }:
            block_reasons.append("confirmation_source_not_allowed")
        if state["confirmed_text"] != state["portuguese_text"] and row.get("output_equals_confirmed_canonical") is not True:
            block_reasons.append("output_not_equal_confirmed")
        built.append(
            {
                "segment_id": segment_id,
                "relative_path": row["relative_path"],
                "source_key": row["source_key"],
                "source_line_number": None,
                "confirmation_label": state["confirmation_label"],
                "policy_action": POLICY_ACTION,
                "policy_allowed": 0 if block_reasons else 1,
                "block_reason": ";".join(block_reasons),
                "output_match_kind": "canonical_l10n_output_confirmation_match",
                "token_status": "ok",
                "issue_count": 0,
                "high_issue_count": 0,
                "dry_run_source": str(DRY_RUN_JSONL),
            }
        )
    return built


def output_paths(mode: str) -> tuple[Path, Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_phase1_human_package_close_bridge_materializer_{mode}"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), Path(str(base) + "_summary.json"), base.with_suffix(".csv")


def apply_policy(conn: sqlite3.Connection, rows: list[dict[str, Any]], txt_path: Path, jsonl_path: Path, csv_path: Path) -> int:
    started = now()
    released = sum(1 for row in rows if row["policy_allowed"])
    cursor = conn.execute(
        """
        INSERT INTO auto_confirmation_reopen_lifecycle_policy_runs (
            rule_version, queue_run_id, audit_run_id, policy_name, label_family,
            policy_status, candidate_count, released_count, blocked_count,
            manual_boundary_count, invalid_count, report_path, csv_path, jsonl_path,
            started_at, finished_at, updated_at
        )
        VALUES (?, NULL, NULL, ?, ?, 'active', ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            SOURCE,
            POLICY_NAME,
            LABEL_FAMILY,
            len(rows),
            released,
            len(rows) - released,
            len(rows) - released,
            str(txt_path),
            str(csv_path),
            str(jsonl_path),
            started,
            started,
            started,
        ),
    )
    run_id = int(cursor.lastrowid)
    for row in rows:
        conn.execute(
            """
            INSERT INTO auto_confirmation_reopen_lifecycle_policy_items (
                run_id, queue_run_id, queue_item_id, audit_run_id, audit_item_id,
                segment_id, relative_path, source_key, source_line_number,
                label_family, confirmation_label, policy_action, policy_allowed,
                block_reason, output_match_kind, token_status, issue_count,
                high_issue_count, model_safe_probability, review_priority,
                reasons_json, created_at
            )
            VALUES (?, NULL, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
            """,
            (
                run_id,
                row["segment_id"],
                row["relative_path"],
                row["source_key"],
                row["source_line_number"],
                LABEL_FAMILY,
                row["confirmation_label"],
                POLICY_ACTION,
                int(row["policy_allowed"]),
                row["block_reason"],
                row["output_match_kind"],
                row["token_status"],
                int(row["issue_count"]),
                int(row["high_issue_count"]),
                0.0,
                json.dumps({"source": SOURCE, "dry_run_jsonl": str(DRY_RUN_JSONL), "segment_state_run_id": SEGMENT_STATE_RUN_ID}, ensure_ascii=False, sort_keys=True),
                started,
            ),
        )
    return run_id


def write_reports(rows: list[dict[str, Any]], *, mode: str, policy_run_id: int | None) -> tuple[Path, Path, Path, Path, dict[str, Any]]:
    txt_path, jsonl_path, summary_path, csv_path = output_paths(mode)
    released = sum(1 for row in rows if row["policy_allowed"])
    blocked = len(rows) - released
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": mode,
        "policy_name": POLICY_NAME,
        "policy_action": POLICY_ACTION,
        "policy_run_id": policy_run_id,
        "segment_state_run_id": SEGMENT_STATE_RUN_ID,
        "candidate_count": len(rows),
        "released_count": released,
        "blocked_count": blocked,
        "confirmation_label_counts": dict(Counter(row["confirmation_label"] for row in rows)),
        "expected_delta_after_segment_state": {
            "closed_count": released,
            "pending_count": -released,
            "reopen_count": -released,
            "output_apply_pending_count": 0,
        },
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "writes_source": False,
        "writes_output": False,
        "production_full_recommended_now": False,
    }
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    with csv_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("segment_id,policy_allowed,block_reason,relative_path,source_key,confirmation_label\n")
        for row in rows:
            handle.write(
                f"{row['segment_id']},{row['policy_allowed']},\"{row['block_reason']}\",\"{row['relative_path']}\",\"{row['source_key']}\",\"{row['confirmation_label']}\"\n"
            )
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    txt_path.write_text(
        "\n".join(
            [
                f"Phase1 human package close bridge materializer {mode}",
                f"policy_run_id={policy_run_id or ''}",
                f"candidate_count={len(rows)}",
                f"released_count={released}",
                f"blocked_count={blocked}",
                "candidate_generation_count=0",
                "apply_count=0",
                "lifecycle_count=0",
                "segment_state_count=0",
                "reindex_count=0",
                "production_full_count=0",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return txt_path, jsonl_path, summary_path, csv_path, summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    mode = "apply" if args.apply else "dry_run"
    dry_summary = read_json(DRY_RUN_SUMMARY)
    dry_rows = read_jsonl(DRY_RUN_JSONL)
    validate_inputs(dry_summary, dry_rows)
    with connect_readonly() as conn:
        state_guards = fetch_state_guards(conn, dry_rows)
    rows = build_rows(dry_rows, state_guards)
    if sum(1 for row in rows if row["policy_allowed"]) != EXPECTED_COUNT:
        write_reports(rows, mode=mode, policy_run_id=None)
        raise SystemExit("materializer guard failed: not all rows allowed")
    policy_run_id = None
    if args.apply:
        txt_path, jsonl_path, summary_path, csv_path = output_paths(mode)
        with db.connect(db.load_settings()) as conn:
            policy_run_id = apply_policy(conn, rows, txt_path, jsonl_path, csv_path)
            conn.commit()
        # Re-write reports after policy id is known, keeping DB report paths stable is less important than a clear summary.
    txt_path, jsonl_path, summary_path, csv_path, summary = write_reports(rows, mode=mode, policy_run_id=policy_run_id)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"csv={csv_path}")
    print(f"mode={mode}")
    print(f"policy_run_id={policy_run_id or ''}")
    print(f"candidate_count={summary['candidate_count']}")
    print(f"released_count={summary['released_count']}")
    print(f"blocked_count={summary['blocked_count']}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
