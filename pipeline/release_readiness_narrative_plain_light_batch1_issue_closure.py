from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import canonical_localization_text


SOURCE = "release_readiness_narrative_plain_light_batch1_issue_closure_v1"
APPLY_JSONL = Path("reports/20260703_011310_587569_release_readiness_narrative_plain_light_batch1_apply_apply.jsonl")
DEFAULT_LEDGER_RUN_ID = 76
TARGET_STATUS = "closed"
TARGET_VALIDATION_STATUS = "closed_resolved_by_human_narrative_plain_light_batch1_corrected"


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Close issues resolved by narrative plain/light batch1 corrected apply.")
    parser.add_argument("--apply-jsonl", type=Path, default=APPLY_JSONL)
    parser.add_argument("--ledger-run-id", type=int, default=DEFAULT_LEDGER_RUN_ID)
    parser.add_argument("--target-validation-status", default=TARGET_VALIDATION_STATUS)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def read_ids(path: Path) -> list[int]:
    ids: list[int] = []
    with db.project_path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                row = json.loads(line)
                if row.get("status") == "ready":
                    ids.append(int(row["segment_id"]))
    return sorted(set(ids))


def build_records(conn, ids: list[int], ledger_run_id: int, target_validation_status: str) -> list[dict[str, Any]]:
    ph = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT issue.id AS issue_id, issue.run_id AS issue_run_id, issue.segment_id,
               issue.relative_path, issue.source_key, issue.issue_family, issue.issue_kind,
               issue.issue_severity, issue.status AS old_status, issue.validation_status AS old_validation_status,
               output.portuguese_text AS output_text, confirmation.confirmed_text, confirmation.locked,
               confirmation.confirmation_level
        FROM ml_issue_ledger_items issue
        JOIN output_segments output ON output.segment_id=issue.segment_id
        JOIN segment_confirmations confirmation ON confirmation.segment_id=issue.segment_id
        WHERE issue.run_id=?
          AND issue.segment_id IN ({ph})
          AND COALESCE(issue.status,'open') NOT IN ('closed','resolved','dismissed')
        ORDER BY issue.segment_id, issue.id
        """,
        [ledger_run_id, *ids],
    ).fetchall()
    records = []
    for row in rows:
        r = dict(row)
        reasons: list[str] = []
        if int(r.get("locked") or 0) != 1 or r.get("confirmation_level") != "human_confirmed":
            reasons.append("not_human_locked")
        if canonical_localization_text(r.get("output_text") or "") != canonical_localization_text(r.get("confirmed_text") or ""):
            reasons.append("canonical_output_not_confirmed")
        records.append({
            "source": SOURCE,
            "record_type": "issue_closure_item",
            "issue_id": int(r["issue_id"]),
            "issue_run_id": int(r["issue_run_id"]),
            "segment_id": int(r["segment_id"]),
            "relative_path": r["relative_path"],
            "source_key": r["source_key"],
            "issue_family": r["issue_family"],
            "issue_kind": r["issue_kind"],
            "issue_severity": r["issue_severity"],
            "old_status": r["old_status"],
            "old_validation_status": r["old_validation_status"],
            "new_status": TARGET_STATUS,
            "new_validation_status": target_validation_status,
            "dry_run_decision": "released" if not reasons else "blocked",
            "block_reasons": reasons,
            "candidate_generation_count": 0,
            "apply_count": 0,
            "lifecycle_count": 0,
            "segment_state_count": 0,
            "reindex_count": 0,
            "production_full_count": 0,
        })
    return records


def apply_records(conn, records: list[dict[str, Any]]) -> None:
    for record in records:
        conn.execute(
            "UPDATE ml_issue_ledger_items SET status=?, validation_status=? WHERE id=? AND segment_id=?",
            (TARGET_STATUS, TARGET_VALIDATION_STATUS, int(record["issue_id"]), int(record["segment_id"])),
        )
    conn.commit()


def write(records: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    base = reports_dir() / f"{stamp()}_release_readiness_narrative_plain_light_batch1_issue_closure_{summary['mode']}"
    txt = base.with_suffix(".txt")
    jsonl = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {"txt": str(txt), "jsonl": str(jsonl), "summary": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    txt.write_text("\n".join([
        "Narrative plain/light batch1 issue closure",
        f"mode={summary['mode']}",
        f"issue_count={summary['issue_count']}",
        f"segment_count={summary['segment_count']}",
        f"released_count={summary['released_count']}",
        f"blocked_count={summary['blocked_count']}",
        f"closed_issue_count={summary['closed_issue_count']}",
        f"block_reason_counts={json.dumps(summary['block_reason_counts'], ensure_ascii=False, sort_keys=True)}",
        "candidate_generation_count=0",
        "apply_count=0",
        "lifecycle_count=0",
        "segment_state_count=0",
        "reindex_count=0",
        "production_full_count=0",
    ]) + "\n", encoding="utf-8")
    print(f"txt={txt}")
    print(f"jsonl={jsonl}")
    print(f"summary={summary_path}")


def main() -> None:
    args = parse_args()
    mode = "apply" if args.apply else "dry_run"
    ids = read_ids(args.apply_jsonl)
    with db.connect(db.load_settings()) as conn:
        records = build_records(conn, ids, args.ledger_run_id, args.target_validation_status)
        blocked = [r for r in records if r["dry_run_decision"] != "released"]
        released = [r for r in records if r["dry_run_decision"] == "released"]
        if args.apply:
            if blocked:
                raise SystemExit("blocked records present; refusing apply")
            apply_records(conn, released)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": mode,
        "apply_jsonl": str(args.apply_jsonl),
        "ledger_run_id": args.ledger_run_id,
        "target_validation_status": args.target_validation_status,
        "target_segment_ids": ids,
        "issue_count": len(records),
        "segment_count": len(set(r["segment_id"] for r in records)),
        "released_count": len(released),
        "blocked_count": len(blocked),
        "closed_issue_count": len(released) if args.apply else 0,
        "issue_family_counts": dict(Counter(r["issue_family"] for r in records).most_common()),
        "block_reason_counts": dict(Counter(x for r in blocked for x in r["block_reasons"]).most_common()),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
    }
    write(records, summary)
    print(f"mode={mode}")
    print(f"issue_count={summary['issue_count']}")
    print(f"segment_count={summary['segment_count']}")
    print(f"released_count={summary['released_count']}")
    print(f"blocked_count={summary['blocked_count']}")
    print(f"closed_issue_count={summary['closed_issue_count']}")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
