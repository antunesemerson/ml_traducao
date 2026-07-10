from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "release_readiness_ui_tooltips_final6_issue_closure_v1"
SEGMENT_IDS = [50666, 60232, 69330, 160244, 160251, 160493]
LEDGER_RUN_ID = 77


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Close resolved/no-op UI tooltips final6 issues if any remain open.")
    parser.add_argument("--ledger-run-id", type=int, default=LEDGER_RUN_ID)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def fetch_open_issues(conn, ledger_run_id: int) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in SEGMENT_IDS)
    rows = conn.execute(
        f"""
        SELECT *
        FROM ml_issue_ledger_items
        WHERE run_id=?
          AND segment_id IN ({placeholders})
          AND COALESCE(status, 'open') NOT IN ('closed', 'resolved', 'dismissed')
        ORDER BY segment_id, id
        """,
        [ledger_run_id, *SEGMENT_IDS],
    ).fetchall()
    return [dict(row) for row in rows]


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    base = reports_dir() / f"{stamp()}_release_readiness_ui_tooltips_final6_issue_closure_{summary['mode']}"
    txt = base.with_suffix(".txt")
    jsonl = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {"txt": str(txt), "jsonl": str(jsonl), "summary": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    txt.write_text(
        "\n".join(
            [
                "Release readiness UI/tooltips final6 issue closure",
                f"mode={summary['mode']}",
                f"issue_count={summary['issue_count']}",
                f"closed_issue_count={summary['closed_issue_count']}",
                f"issue_family_counts={json.dumps(summary['issue_family_counts'], ensure_ascii=False, sort_keys=True)}",
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
    print(f"txt={txt}")
    print(f"jsonl={jsonl}")
    print(f"summary={summary_path}")


def main() -> None:
    args = parse_args()
    mode = "apply" if args.apply else "dry_run"
    timestamp = now()
    with db.connect(db.load_settings()) as conn:
        issues = fetch_open_issues(conn, args.ledger_run_id)
        records = [
            {
                "source": SOURCE,
                "record_type": "issue_closure_item",
                "issue_id": issue.get("id"),
                "segment_id": issue.get("segment_id"),
                "issue_family": issue.get("issue_family"),
                "issue_kind": issue.get("issue_kind"),
                "old_status": issue.get("status"),
                "new_status": "closed",
                "closure_reason": "human_final6_no_open_issue_or_output_already_confirmed",
                "candidate_generation_count": 0,
                "apply_count": 0,
                "lifecycle_count": 0,
                "segment_state_count": 0,
                "reindex_count": 0,
                "production_full_count": 0,
            }
            for issue in issues
        ]
        if args.apply and records:
            ids = [int(record["issue_id"]) for record in records]
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"""
                UPDATE ml_issue_ledger_items
                SET status='closed',
                    resolution='resolved_by_human_ui_tooltips_final6',
                    resolved_at=?,
                    updated_at=?
                WHERE id IN ({placeholders})
                """,
                [timestamp, timestamp, *ids],
            )
            conn.commit()
    family_counts = Counter(record["issue_family"] or "none" for record in records)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": mode,
        "ledger_run_id": args.ledger_run_id,
        "segment_ids": SEGMENT_IDS,
        "issue_count": len(records),
        "closed_issue_count": len(records) if args.apply else 0,
        "issue_family_counts": dict(family_counts.most_common()),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
    }
    write_reports(records, summary)
    print(f"mode={mode}")
    print(f"issue_count={summary['issue_count']}")
    print(f"closed_issue_count={summary['closed_issue_count']}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
