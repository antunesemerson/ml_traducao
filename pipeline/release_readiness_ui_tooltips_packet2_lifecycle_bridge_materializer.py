from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import db
from release_readiness_ui_tooltips_packet2_lifecycle_bridge_dry_run import (
    FINAL_STATE,
    POLICY_ACTION,
    POLICY_NAME,
    SOURCE as DRY_RUN_SOURCE,
    TARGET_SEGMENT_IDS,
    build_records as build_dry_run_records,
    build_summary as build_dry_run_summary,
)


SOURCE = "release_readiness_ui_tooltips_packet2_lifecycle_bridge_materializer_v1"
LABEL_FAMILY = "human_confirmed_ui_tooltips_packet2_corrected_equal_output"
DEFAULT_SEGMENT_STATE_RUN_ID = 555
DEFAULT_LEDGER_RUN_ID = 76
EXPECTED_COUNT = 6


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db.get_database_path(db.load_settings()), timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Materialize narrow UI/tooltips packet2 lifecycle bridge.")
    parser.add_argument("--segment-state-run-id", type=int, default=DEFAULT_SEGMENT_STATE_RUN_ID)
    parser.add_argument("--ledger-run-id", type=int, default=DEFAULT_LEDGER_RUN_ID)
    parser.add_argument("--expected-count", type=int, default=EXPECTED_COUNT)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def validate_dry_run(records: list[dict[str, Any]], summary: dict[str, Any], args: argparse.Namespace) -> None:
    if int(summary.get("record_count") or 0) != args.expected_count:
        raise SystemExit("record_count guard failed")
    if int(summary.get("released_count") or 0) != args.expected_count:
        raise SystemExit("released_count guard failed")
    if int(summary.get("blocked_count") or 0) != 0:
        raise SystemExit("blocked_count guard failed")
    if sorted(int(row["segment_id"]) for row in records) != sorted(TARGET_SEGMENT_IDS):
        raise SystemExit("target segment ids guard failed")
    for row in records:
        if row.get("dry_run_decision") != "released":
            raise SystemExit(f"non-released dry-run row: {row.get('segment_id')}")
        if row.get("policy_name") != POLICY_NAME or row.get("policy_action") != POLICY_ACTION:
            raise SystemExit("policy guard failed")
        if row.get("review_state") != "human_locked":
            raise SystemExit("review_state guard failed")
        if row.get("confirmation_level") != "human_confirmed" or int(row.get("locked") or 0) != 1:
            raise SystemExit("confirmation guard failed")
        if int(row.get("confirmed_matches_output") or 0) != 1 or int(row.get("needs_output_apply") or 0) != 0:
            raise SystemExit("output state guard failed")
        if int(row.get("open_issue_count") or 0) != 0 or int(row.get("high_issue_count") or 0) != 0:
            raise SystemExit("issue guard failed")
        if int(row.get("existing_policy_item_count") or 0) != 0:
            raise SystemExit("existing policy guard failed")


def insert_policy(conn: sqlite3.Connection, rows: list[dict[str, Any]], txt_path: Path, jsonl_path: Path) -> int:
    started = now_iso()
    cursor = conn.execute(
        """
        INSERT INTO auto_confirmation_reopen_lifecycle_policy_runs (
            rule_version, queue_run_id, audit_run_id, policy_name, label_family,
            policy_status, candidate_count, released_count, blocked_count,
            manual_boundary_count, invalid_count, report_path, csv_path, jsonl_path,
            started_at, finished_at, updated_at
        )
        VALUES (?, NULL, NULL, ?, ?, 'active', ?, ?, 0, 0, 0, ?, NULL, ?, ?, ?, ?)
        """,
        (
            SOURCE,
            POLICY_NAME,
            LABEL_FAMILY,
            len(rows),
            len(rows),
            str(txt_path),
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
            VALUES (?, NULL, NULL, NULL, NULL, ?, ?, ?, NULL, ?, ?, ?, 1, NULL, ?, ?, 0, 0, ?, ?, ?, ?)
            """,
            (
                run_id,
                int(row["segment_id"]),
                row["relative_path"],
                row["source_key"],
                LABEL_FAMILY,
                row["confirmation_label"],
                POLICY_ACTION,
                "canonical_l10n_output_confirmation_match",
                "ok",
                1.0,
                0.0,
                json.dumps(
                    {
                        "source": SOURCE,
                        "validated_by": DRY_RUN_SOURCE,
                        "segment_state_run_id": DEFAULT_SEGMENT_STATE_RUN_ID,
                        "ledger_run_id": DEFAULT_LEDGER_RUN_ID,
                        "expected_final_state": FINAL_STATE,
                        "token_surface": row.get("token_surface"),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                started,
            ),
        )
    conn.commit()
    return run_id


def output_paths(mode: str) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_release_readiness_ui_tooltips_packet2_lifecycle_bridge_materializer_{mode}"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), Path(str(base) + "_summary.json")


def write_reports(
    rows: list[dict[str, Any]],
    summary: dict[str, Any],
    mode: str,
    policy_run_id: int | None,
    txt_path: Path,
    jsonl_path: Path,
    summary_path: Path,
) -> None:
    released = len(rows)
    out_summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": mode,
        "policy_name": POLICY_NAME,
        "policy_action": POLICY_ACTION,
        "expected_final_state": FINAL_STATE,
        "policy_run_id": policy_run_id,
        "segment_state_run_id": summary["segment_state_run_id"],
        "ledger_run_id": summary["ledger_run_id"],
        "candidate_count": len(rows),
        "released_count": released,
        "blocked_count": 0,
        "released_segment_ids": [int(row["segment_id"]) for row in rows],
        "token_surface_counts": dict(Counter(row["token_surface"] for row in rows)),
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
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
    }
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(
                json.dumps(
                    {
                        "record_type": "policy_item",
                        "policy_name": POLICY_NAME,
                        "policy_action": POLICY_ACTION,
                        "expected_final_state": FINAL_STATE,
                        "policy_allowed": 1,
                        **row,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )
    out_summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    summary_path.write_text(json.dumps(out_summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    txt_path.write_text(
        "\n".join(
            [
                "UI/tooltips packet2 lifecycle bridge materializer",
                f"mode={mode}",
                f"policy_run_id={policy_run_id}",
                f"policy_name={POLICY_NAME}",
                f"policy_action={POLICY_ACTION}",
                f"expected_final_state={FINAL_STATE}",
                f"candidate_count={len(rows)}",
                f"released_count={released}",
                "blocked_count=0",
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


def main() -> None:
    args = parse_args()
    mode = "apply" if args.apply else "dry_run"
    with connect() as conn:
        dry_args = argparse.Namespace(
            segment_state_run_id=args.segment_state_run_id,
            ledger_run_id=args.ledger_run_id,
            expected_count=args.expected_count,
        )
        records = build_dry_run_records(conn, dry_args)
        dry_summary = build_dry_run_summary(records, dry_args)
        validate_dry_run(records, dry_summary, args)
        txt_path, jsonl_path, summary_path = output_paths(mode)
        policy_run_id = insert_policy(conn, records, txt_path, jsonl_path) if args.apply else None
        write_reports(records, dry_summary, mode, policy_run_id, txt_path, jsonl_path, summary_path)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"mode={mode}")
    print(f"policy_run_id={policy_run_id or ''}")
    print(f"candidate_count={len(records)}")
    print(f"released_count={len(records)}")
    print("blocked_count=0")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
