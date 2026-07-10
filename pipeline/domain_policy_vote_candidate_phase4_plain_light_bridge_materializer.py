from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import db
from domain_policy_vote_candidate_phase4_plain_light_bridge_dry_run import (
    EXPECTED_RELEASED,
    POLICY_ACTION,
    POLICY_NAME,
    SOURCE as DRY_RUN_SOURCE,
    TARGET_FINAL_STATE,
    build as build_dry_run,
)


SOURCE = "domain_policy_vote_candidate_phase4_plain_light_bridge_materializer_v1"
INPUT_JSONL = Path("reports/20260702_004603_514470_domain_policy_vote_candidate_debug_consumption_phase4_readonly_review_run538.jsonl")
RUN_ID = 538
LABEL_FAMILY = "domain_policy_vote_candidate_phase4_plain_light_equal_output"


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Materialize phase 4 plain/light equal-output lifecycle bridge.")
    parser.add_argument("--input-jsonl", type=Path, default=INPUT_JSONL)
    parser.add_argument("--run-id", type=int, default=RUN_ID)
    parser.add_argument("--expected-released", type=int, default=EXPECTED_RELEASED)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db.get_database_path(db.load_settings()), timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


def insert_policy_run(conn: sqlite3.Connection, records: list[dict[str, Any]], args: argparse.Namespace) -> int:
    started = now_iso()
    cur = conn.execute(
        """
        INSERT INTO auto_confirmation_reopen_lifecycle_policy_runs (
          rule_version, queue_run_id, audit_run_id, policy_name, label_family, policy_status,
          candidate_count, released_count, blocked_count, manual_boundary_count, invalid_count,
          report_path, csv_path, jsonl_path, started_at, finished_at, updated_at
        ) VALUES (?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, ?)
        """,
        (
            SOURCE,
            POLICY_NAME,
            LABEL_FAMILY,
            "released",
            args.expected_released,
            args.expected_released,
            0,
            0,
            0,
            started,
            started,
            started,
        ),
    )
    policy_run_id = int(cur.lastrowid)
    for record in records:
        conn.execute(
            """
            INSERT INTO auto_confirmation_reopen_lifecycle_policy_items (
              run_id, queue_run_id, queue_item_id, audit_run_id, audit_item_id,
              segment_id, relative_path, source_key, source_line_number, label_family,
              confirmation_label, policy_action, policy_allowed, block_reason,
              output_match_kind, token_status, issue_count, high_issue_count,
              model_safe_probability, review_priority, reasons_json, created_at
            ) VALUES (?, NULL, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                policy_run_id,
                record["segment_id"],
                record["relative_path"],
                record["source_key"],
                record["source_line_number"],
                LABEL_FAMILY,
                record["confirmation_label"],
                POLICY_ACTION,
                1,
                None,
                "canonical_equal",
                "ok",
                0,
                0,
                1.0,
                0,
                json.dumps(
                    {
                        "source": SOURCE,
                        "validated_by": DRY_RUN_SOURCE,
                        "guarded_segment_state_run_id": args.run_id,
                        "target_final_state": TARGET_FINAL_STATE,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                started,
            ),
        )
    conn.commit()
    return policy_run_id


def write_reports(summary: dict[str, Any], records: list[dict[str, Any]]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_phase4_plain_light_bridge_materializer_{'apply' if summary['apply'] else 'dry_run'}"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(
                json.dumps(
                    {
                        "record_type": "policy_item",
                        "policy_name": POLICY_NAME,
                        "policy_action": POLICY_ACTION,
                        "target_final_state": TARGET_FINAL_STATE,
                        **record,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "Phase 4 plain/light equal-output bridge materializer",
        f"apply={summary['apply']}",
        f"policy_run_id={summary.get('policy_run_id')}",
        f"policy_name={POLICY_NAME}",
        f"policy_action={POLICY_ACTION}",
        f"target_final_state={TARGET_FINAL_STATE}",
        f"released_count={summary['released_count']}",
        f"blocked_count={summary['blocked_count']}",
        "",
        "Operation counters:",
        "candidate_generation_count=0",
        "apply_count=0",
        "lifecycle_count=0",
        "segment_state_count=0",
        "reindex_count=0",
        "production_full_count=0",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    args = parse_args()
    dry_args = argparse.Namespace(input_jsonl=args.input_jsonl, run_id=args.run_id, expected_released=args.expected_released)
    records, dry_summary = build_dry_run(dry_args)
    released_records = [record for record in records if record["record_type"] == "released"]
    blocked_records = [record for record in records if record["record_type"] == "blocked"]
    if len(released_records) != args.expected_released or blocked_records:
        raise SystemExit(f"guard failed: released={len(released_records)} blocked={len(blocked_records)}")

    policy_run_id = None
    if args.apply:
        with connect() as conn:
            policy_run_id = insert_policy_run(conn, released_records, args)

    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "apply" if args.apply else "dry_run",
        "apply": bool(args.apply),
        "input_jsonl": str(args.input_jsonl),
        "run_id": args.run_id,
        "policy_name": POLICY_NAME,
        "policy_action": POLICY_ACTION,
        "target_final_state": TARGET_FINAL_STATE,
        "policy_run_id": policy_run_id,
        "released_count": len(released_records),
        "blocked_count": len(blocked_records),
        "expected_released": args.expected_released,
        "dry_run_expected_outcome_met": bool(dry_summary.get("expected_outcome_met")),
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
            "Bridge materialized. Next step: run only segment-state and delta against run 538."
            if args.apply
            else "Dry-run passed. Re-run with --apply to materialize only this bridge."
        ),
    }
    txt_path, jsonl_path, summary_path = write_reports(summary, released_records)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"policy_run_id={policy_run_id}")
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
