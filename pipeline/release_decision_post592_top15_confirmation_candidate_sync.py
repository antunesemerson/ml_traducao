from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "release_decision_post592_top15_confirmation_candidate_sync_v1"
LEARNING_RUN_ID = 808
EXPECTED_COUNT = 13
CONFIRMATION_SOURCE = "codex_release_readiness_human_review"
CONFIRMATION_LABEL = "narrative_plain_light_batch_post592_top15_corrected"


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Guarded sync of segment_confirmations.candidate_id for post592 top15.")
    parser.add_argument("--learning-run-id", type=int, default=LEARNING_RUN_ID)
    parser.add_argument("--expected-count", type=int, default=EXPECTED_COUNT)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def fetch_rows(conn, run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
          ll.id AS candidate_id,
          ll.run_id,
          ll.segment_id,
          ll.suggested_text,
          c.id AS confirmation_id,
          c.confirmed_text,
          c.confirmation_source,
          c.confirmation_label,
          c.locked,
          c.candidate_id AS old_candidate_id,
          o.portuguese_text AS output_text
        FROM local_learning_candidates ll
        JOIN segment_confirmations c ON c.segment_id = ll.segment_id
        JOIN output_segments o ON o.segment_id = ll.segment_id
        WHERE ll.run_id = ?
        ORDER BY ll.segment_id
        """,
        (run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def classify(row: dict[str, Any]) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if row.get("confirmation_source") != CONFIRMATION_SOURCE:
        reasons.append("confirmation_source_mismatch")
    if row.get("confirmation_label") != CONFIRMATION_LABEL:
        reasons.append("confirmation_label_mismatch")
    if int(row.get("locked") or 0) != 1:
        reasons.append("not_locked")
    if row.get("confirmed_text") != row.get("suggested_text"):
        reasons.append("confirmed_not_suggested")
    if row.get("output_text") != row.get("confirmed_text"):
        reasons.append("output_not_confirmed")
    old_candidate_id = row.get("old_candidate_id")
    if old_candidate_id not in (None, row.get("candidate_id")):
        reasons.append("different_candidate_already_linked")
    return ("ready" if not reasons else "blocked"), reasons


def write_reports(summary: dict[str, Any], records: list[dict[str, Any]]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_release_decision_post592_top15_confirmation_candidate_sync_{summary['mode']}"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    txt_path.write_text(
        "\n".join(
            [
                "Post592 top15 confirmation candidate sync",
                f"mode={summary['mode']}",
                f"record_count={summary['record_count']}",
                f"ready_count={summary['ready_count']}",
                f"blocked_count={summary['blocked_count']}",
                f"synced_count={summary['synced_count']}",
                f"block_reason_counts={json.dumps(summary['block_reason_counts'], ensure_ascii=False, sort_keys=True)}",
                "candidate_generation_count=0",
                "apply_count=0",
                "output_apply_count=0",
                "issue_closure_count=0",
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
    mode = "apply" if args.apply else "dry_run"
    with db.connect(db.load_settings()) as conn:
        rows = fetch_rows(conn, args.learning_run_id)
        if len(rows) != args.expected_count:
            raise SystemExit(f"expected {args.expected_count}, got {len(rows)}")
        records: list[dict[str, Any]] = []
        for row in rows:
            status, reasons = classify(row)
            records.append({**row, "source": SOURCE, "status": status, "block_reasons": reasons})
        blocked = [record for record in records if record["status"] != "ready"]
        synced = 0
        if args.apply and blocked:
            raise SystemExit("blocked records present; refusing apply")
        if args.apply:
            for record in records:
                conn.execute(
                    "UPDATE segment_confirmations SET candidate_id = ?, updated_at = ? WHERE id = ?",
                    (int(record["candidate_id"]), datetime.now().isoformat(timespec="seconds"), int(record["confirmation_id"])),
                )
                synced += 1
            conn.commit()
    block_counts = Counter(reason for record in blocked for reason in record["block_reasons"])
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": mode,
        "learning_run_id": args.learning_run_id,
        "record_count": len(records),
        "ready_count": len(records) - len(blocked),
        "blocked_count": len(blocked),
        "synced_count": synced,
        "block_reason_counts": dict(block_counts.most_common()),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "output_apply_count": 0,
        "issue_closure_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
    }
    txt, jsonl, summary_path = write_reports(summary, records)
    print(f"txt={txt}")
    print(f"jsonl={jsonl}")
    print(f"summary={summary_path}")
    print(f"mode={mode}")
    print(f"record_count={summary['record_count']}")
    print(f"ready_count={summary['ready_count']}")
    print(f"blocked_count={summary['blocked_count']}")
    print(f"synced_count={summary['synced_count']}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("output_apply_count=0")
    print("issue_closure_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
