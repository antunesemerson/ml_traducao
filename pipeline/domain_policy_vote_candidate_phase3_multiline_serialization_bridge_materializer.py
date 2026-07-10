from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_phase3_multiline_serialization_bridge_materializer_v1"
DRY_RUN_JSONL = Path(
    "reports/20260702_121600_743601_domain_policy_vote_candidate_phase3_multiline_serialization_bridge_dry_run.jsonl"
)
DRY_RUN_SUMMARY = Path(
    "reports/20260702_121600_743601_domain_policy_vote_candidate_phase3_multiline_serialization_bridge_dry_run_summary.json"
)
SEGMENT_STATE_RUN_ID = 541
EXPECTED_COUNT = 1118
POLICY_NAME = "human_confirmed_misc_equal_output_multiline_serialization_lifecycle_bridge"
POLICY_ACTION = "close_reopen_human_confirmed_misc_equal_output_multiline_serialization_lifecycle"
FINAL_STATE = "closed_auto_confirmed_human_confirmed_misc_equal_output_multiline_serialization_lifecycle"
LABEL_FAMILY = "human_confirmed_misc_equal_output_multiline_serialization"
ALLOWED_CLASSES = {"serialization_only_candidate", "serialization_equal_ui_block"}


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Materialize phase3 multiline serialization lifecycle bridge.")
    parser.add_argument("--dry-run-jsonl", type=Path, default=DRY_RUN_JSONL)
    parser.add_argument("--dry-run-summary", type=Path, default=DRY_RUN_SUMMARY)
    parser.add_argument("--segment-state-run-id", type=int, default=SEGMENT_STATE_RUN_ID)
    parser.add_argument("--expected-count", type=int, default=EXPECTED_COUNT)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


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


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db.get_database_path(db.load_settings()), timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


def validate_dry_run(summary: dict[str, Any], rows: list[dict[str, Any]], args: argparse.Namespace) -> None:
    expected = {
        "record_count": args.expected_count,
        "released_count": args.expected_count,
        "blocked_count": 0,
        "consumer_supported_now_count": args.expected_count,
        "segment_state_run_id": args.segment_state_run_id,
    }
    for key, value in expected.items():
        if int(summary.get(key) or 0) != value:
            raise SystemExit(f"summary guard failed: {key}={summary.get(key)} expected {value}")
    if len(rows) != args.expected_count:
        raise SystemExit(f"row count guard failed: {len(rows)}")
    for row in rows:
        if row.get("dry_run_decision") != "released":
            raise SystemExit(f"non-released row in dry-run: {row.get('segment_id')}")
        if row.get("recommended_policy_name") != POLICY_NAME:
            raise SystemExit("policy_name guard failed")
        if row.get("recommended_policy_action") != POLICY_ACTION:
            raise SystemExit("policy_action guard failed")
        if row.get("classification") not in ALLOWED_CLASSES:
            raise SystemExit("classification guard failed")
        if int(row.get("has_effect_or_debug_marker") or 0) != 0:
            raise SystemExit("effect/debug guard failed")
        if int(row.get("state_confirmed_matches_output") or 0) != 1:
            raise SystemExit("state confirmed_matches_output guard failed")
        if int(row.get("state_needs_output_apply") or 0) != 0:
            raise SystemExit("state needs_output_apply guard failed")
        if row.get("state_final_state") != "reopen_auto_confirmed_autofix":
            raise SystemExit("state final_state guard failed")


def insert_policy_run(conn: sqlite3.Connection, rows: list[dict[str, Any]], args: argparse.Namespace) -> int:
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
            args.expected_count,
            args.expected_count,
            0,
            0,
            0,
            started,
            started,
            started,
        ),
    )
    policy_run_id = int(cur.lastrowid)
    for row in rows:
        conn.execute(
            """
            INSERT INTO auto_confirmation_reopen_lifecycle_policy_items (
              run_id, queue_run_id, queue_item_id, audit_run_id, audit_item_id,
              segment_id, relative_path, source_key, source_line_number, label_family,
              confirmation_label, policy_action, policy_allowed, block_reason,
              output_match_kind, token_status, issue_count, high_issue_count,
              model_safe_probability, review_priority, reasons_json, created_at
            ) VALUES (?, NULL, NULL, NULL, NULL, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                policy_run_id,
                int(row["segment_id"]),
                row["relative_path"],
                row["source_key"],
                LABEL_FAMILY,
                row["confirmation_label"],
                POLICY_ACTION,
                1,
                None,
                "multiline_serialization_equal",
                "ok",
                0,
                0,
                1.0,
                0,
                json.dumps(
                    {
                        "source": SOURCE,
                        "dry_run_jsonl": str(args.dry_run_jsonl),
                        "guarded_segment_state_run_id": args.segment_state_run_id,
                        "classification": row["classification"],
                        "target_final_state": FINAL_STATE,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                started,
            ),
        )
    conn.commit()
    return policy_run_id


def write_reports(
    summary: dict[str, Any], rows: list[dict[str, Any]], apply_mode: bool, policy_run_id: int | None
) -> tuple[Path, Path, Path]:
    base = reports_dir() / (
        f"{stamp()}_domain_policy_vote_candidate_phase3_multiline_serialization_bridge_materializer_"
        f"{'apply' if apply_mode else 'dry_run'}"
    )
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    records = [
        {
            "record_type": "policy_item",
            "policy_name": POLICY_NAME,
            "policy_action": POLICY_ACTION,
            "target_final_state": FINAL_STATE,
            **row,
        }
        for row in rows
    ]
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    out_summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "apply" if apply_mode else "dry_run",
        "apply": apply_mode,
        "policy_name": POLICY_NAME,
        "policy_action": POLICY_ACTION,
        "target_final_state": FINAL_STATE,
        "policy_run_id": policy_run_id,
        "segment_state_run_id": summary.get("segment_state_run_id"),
        "released_count": len(rows),
        "blocked_count": 0,
        "expected_count": EXPECTED_COUNT,
        "classification_counts": dict(Counter(row["classification"] for row in rows)),
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
            "Bridge materialized. Next step: run only segment-state and delta against run 541."
            if apply_mode
            else "Dry-run passed. Re-run with --apply to materialize only this bridge."
        ),
    }
    out_summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    summary_path.write_text(json.dumps(out_summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    txt_path.write_text(
        "\n".join(
            [
                "Phase3 multiline serialization bridge materializer",
                f"apply={apply_mode}",
                f"policy_run_id={policy_run_id}",
                f"policy_name={POLICY_NAME}",
                f"policy_action={POLICY_ACTION}",
                f"target_final_state={FINAL_STATE}",
                f"released_count={len(rows)}",
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
    return txt_path, jsonl_path, summary_path


def main() -> None:
    args = parse_args()
    summary = read_json(args.dry_run_summary)
    rows = read_jsonl(args.dry_run_jsonl)
    validate_dry_run(summary, rows, args)
    policy_run_id = None
    if args.apply:
        with connect() as conn:
            policy_run_id = insert_policy_run(conn, rows, args)
    txt_path, jsonl_path, summary_path = write_reports(summary, rows, bool(args.apply), policy_run_id)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"policy_run_id={policy_run_id}")
    print(f"released_count={len(rows)}")
    print("blocked_count=0")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
