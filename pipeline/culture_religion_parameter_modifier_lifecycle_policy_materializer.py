from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime
from pathlib import Path

import db
import general_pipe_article_gender_lifecycle_policy_materializer as bridge


RULE_VERSION = "culture_religion_parameter_modifier_lifecycle_policy_materializer_v1"
TARGET_SEGMENT_IDS = (20279, 20281, 20287, 20288)
DEFAULT_SEGMENT_STATE_RUN_ID = 411


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def output_paths(mode: str) -> tuple[Path, Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_culture_religion_parameter_modifier_lifecycle_policy_materializer_{mode}"
    return (
        base.with_suffix(".txt"),
        base.with_suffix(".jsonl"),
        reports_dir() / f"{base.name}_summary.json",
        base.with_suffix(".csv"),
    )


def parse_segment_ids(value: str | None) -> tuple[int, ...]:
    if not value:
        return TARGET_SEGMENT_IDS
    segment_ids = tuple(int(part.strip()) for part in value.split(",") if part.strip())
    if not segment_ids:
        raise SystemExit("empty --segment-ids")
    return segment_ids


def configure_bridge(segment_ids: tuple[int, ...], state_run_id: int) -> None:
    bridge.RULE_VERSION = RULE_VERSION
    bridge.TARGET_SEGMENT_IDS = segment_ids
    bridge.DEFAULT_SEGMENT_STATE_RUN_ID = state_run_id


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--segment-state-run-id", type=int, default=DEFAULT_SEGMENT_STATE_RUN_ID)
    parser.add_argument("--ledger-run-id", type=int)
    parser.add_argument("--segment-ids", help="Comma-separated segment ids. Defaults to batch1 ids.")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    segment_ids = parse_segment_ids(args.segment_ids)
    configure_bridge(segment_ids, args.segment_state_run_id)
    mode = "apply" if args.apply else "dry_run"
    txt_path, jsonl_path, summary_path, csv_path = output_paths(mode)

    if args.apply:
        settings = db.load_settings()
        with db.connect(settings) as conn:
            db.ensure_database(conn)
            conn.row_factory = sqlite3.Row
            ledger_run_id = args.ledger_run_id or bridge.latest_finished_ledger_run_id(conn)
            rows = bridge.collect_rows(conn, args.segment_state_run_id, ledger_run_id)
            if sum(1 for row in rows if row["policy_allowed"]) != len(segment_ids):
                bridge.write_reports(rows, mode, args.segment_state_run_id, ledger_run_id, None, txt_path, jsonl_path, summary_path, csv_path)
                raise SystemExit("apply blocked: not all target segments are policy_allowed")
            policy_run_id = bridge.apply_policy_run(conn, rows, txt_path, csv_path, jsonl_path, args.segment_state_run_id, ledger_run_id)
            summary = bridge.write_reports(rows, mode, args.segment_state_run_id, ledger_run_id, policy_run_id, txt_path, jsonl_path, summary_path, csv_path)
            conn.commit()
    else:
        with bridge.readonly_conn() as conn:
            ledger_run_id = args.ledger_run_id or bridge.latest_finished_ledger_run_id(conn)
            rows = bridge.collect_rows(conn, args.segment_state_run_id, ledger_run_id)
        summary = bridge.write_reports(rows, mode, args.segment_state_run_id, ledger_run_id, None, txt_path, jsonl_path, summary_path, csv_path)

    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"csv={csv_path}")
    print(f"mode={mode}")
    print(f"policy_run_id={summary['policy_run_id'] or ''}")
    print(f"candidate_count={summary['candidate_count']}")
    print(f"released_count={summary['released_count']}")
    print(f"blocked_count={summary['blocked_count']}")
    print("writes_source=false")
    print("writes_output=false")
    print("runs_lifecycle=false")
    print("runs_segment_state=false")


if __name__ == "__main__":
    main()
